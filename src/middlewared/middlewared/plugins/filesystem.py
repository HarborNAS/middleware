import binascii
import errno
import functools
import os
import pathlib
import shutil
import stat as statlib
import time
from typing import Literal

import pyinotify
import truenas_os

from itertools import product
from middlewared.api import api_method
from middlewared.api.base import (
    BaseModel,
    LongNonEmptyString,
    NonEmptyString,
)
from middlewared.api.current import (
    FilesystemListdirArgs, FilesystemListdirResult,
    FilesystemMkdirArgs, FilesystemMkdirResult,
    FilesystemStatArgs, FilesystemStatResult,
    FilesystemStatfsArgs, FilesystemStatfsResult,
    FilesystemSetZfsAttributesArgs, FilesystemSetZfsAttributesResult,
    FilesystemGetZfsAttributesArgs, FilesystemGetZfsAttributesResult,
    FilesystemGetArgs, FilesystemGetResult,
    FilesystemGetArchiveArgs, FilesystemGetArchiveResult,
    FilesystemPutArgs, FilesystemPutResult,
    FilesystemRenameArgs, FilesystemRenameResult,
    FilesystemCopyArgs, FilesystemCopyResult,
    FilesystemMoveArgs, FilesystemMoveResult,
    FilesystemDeleteArgs, FilesystemDeleteResult,
    FileFollowTailEventSourceArgs, FileFollowTailEventSourceEvent,
)
from middlewared.event import EventSource
from middlewared.utils.pwenc import PWENC_FILE_SECRET
from middlewared.plugins.account_.constants import SYNTHETIC_CONTAINER_ROOT, TRUENAS_ADMIN_USERNAME
from middlewared.plugins.docker.state_utils import IX_APPS_DIR_NAME
from middlewared.service import private, CallError, filterable_api_method, Service, job
from middlewared.utils.filter_list import filter_list
from middlewared.utils.filesystem import attrs, stat_x
from middlewared.utils.filesystem.acl import acl_is_present, ACL_UNDEFINED_ID
from middlewared.utils.filesystem.constants import FileType
from middlewared.utils.filesystem.directory import DirectoryIterator, DirectoryRequestMask
from middlewared.utils.io import safe_open
from middlewared.utils.mount import iter_mountinfo, statmount
from middlewared.utils.nss import pwd, grp
from middlewared.utils.path import FSLocation, path_location, is_child_realpath


class FilesystemReceiveFileOptions(BaseModel):
    append: bool = False
    mode: int | None = None
    uid: int = ACL_UNDEFINED_ID
    gid: int = ACL_UNDEFINED_ID


class FilesystemReceiveFileArgs(BaseModel):
    path: NonEmptyString
    content: LongNonEmptyString
    options: FilesystemReceiveFileOptions = FilesystemReceiveFileOptions()


class FilesystemReceiveFileResult(BaseModel):
    result: Literal[True]


class FileFollowTailEventSource(EventSource):
    """
    Retrieve last ``tail_lines`` lines specified as an integer argument for a specified ``path`` and then
    any new lines as they are added.
    """
    args = FileFollowTailEventSourceArgs
    event = FileFollowTailEventSourceEvent

    def run_sync(self):
        path, lines = self.arg['path'], self.arg['tail_lines']

        if not os.path.exists(path):
            # FIXME: Error?
            return

        bufsize = 8192
        fsize = os.stat(path).st_size
        if fsize < bufsize:
            bufsize = fsize
        i = 0
        with safe_open(path, encoding='utf-8', errors='ignore') as f:
            data = []
            while True:
                i += 1
                if bufsize * i > fsize:
                    break
                f.seek(fsize - bufsize * i)
                data.extend(f.readlines())
                if len(data) >= lines or f.tell() == 0:
                    break

            self.send_event('ADDED', fields={'data': ''.join(data[-lines:])})
            f.seek(fsize)

            for data in self._follow_path(path, f):
                self.send_event('ADDED', fields={'data': data})

    def _follow_path(self, path, f):
        queue = []
        watch_manager = pyinotify.WatchManager()
        notifier = pyinotify.Notifier(watch_manager)
        watch_manager.add_watch(path, pyinotify.IN_MODIFY, functools.partial(self._follow_callback, queue, f))

        data = f.read()
        if data:
            yield data

        last_sent_at = time.monotonic()
        interval = 0.5  # For performance reasons do not send websocket events more than twice a second
        while not self._cancel_sync.is_set():
            notifier.process_events()

            if time.monotonic() - last_sent_at >= interval:
                data = "".join(queue)
                if data:
                    yield data
                queue[:] = []
                last_sent_at = time.monotonic()

            if notifier.check_events(timeout=int(interval * 1000)):
                notifier.read_events()

        notifier.stop()

    def _follow_callback(self, queue, f, event):
        data = f.read()
        if data:
            queue.append(data)


class FilesystemService(Service):

    class Config:
        cli_private = True
        event_sources = {
            'filesystem.file_tail_follow': FileFollowTailEventSource,
        }

    @api_method(
        FilesystemSetZfsAttributesArgs, FilesystemSetZfsAttributesResult,
        roles=['FILESYSTEM_ATTRS_WRITE'],
        audit='Filesystem set ZFS attributes',
        audit_extended=lambda data: data['path']
    )
    def set_zfs_attributes(self, data):
        """
        Set special ZFS-related file flags on the specified path

        `readonly` - this maps to READONLY MS-DOS attribute. When set, file may not be
        written to (toggling does not impact existing file opens).

        `hidden` - this maps to HIDDEN MS-DOS attribute. When set, the SMB HIDDEN flag
        is set and file is "hidden" from the perspective of SMB clients.

        `system` - this maps to SYSTEM MS-DOS attribute. Is presented to SMB clients, but
        has no impact on local filesystem.

        `archive` - this maps to ARCHIVE MS-DOS attribute. Value is reset to True whenever
        file is modified.

        `immutable` - file may not be altered or deleted. Also appears as IMMUTABLE in
        attributes in `filesystem.stat` output and as STATX_ATTR_IMMUTABLE in statx() response.

        `nounlink` - file may be altered but not deleted.

        `appendonly` - file may only be opened with O_APPEND flag. Also appears as APPEND in
        attributes in `filesystem.stat` output and as STATX_ATTR_APPEND in statx() response.

        `offline` - this maps to OFFLINE MS-DOS attribute. Is presented to SMB clients, but
        has no impact on local filesystem.

        `sparse` - maps to SPARSE MS-DOS attribute. Is presented to SMB clients, but has
        no impact on local filesystem.
        """
        try:
            return attrs.set_zfs_file_attributes_dict(data['path'], data['zfs_file_attributes'])
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise CallError('Symlinks are not permitted.', errno.ELOOP)
            raise

    @api_method(FilesystemGetZfsAttributesArgs, FilesystemGetZfsAttributesResult, roles=['FILESYSTEM_ATTRS_READ'])
    def get_zfs_attributes(self, path):
        """
        Get the current ZFS attributes for the file at the given path
        """
        try:
            fd = truenas_os.openat2(path, os.O_RDONLY, resolve=truenas_os.RESOLVE_NO_SYMLINKS)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise CallError('Symlinks are not permitted.', errno.ELOOP)
            raise
        try:
            attr_mask = attrs.fget_zfs_file_attributes(fd)
        finally:
            os.close(fd)

        return attrs.zfs_attributes_to_dict(attr_mask)

    @private
    def is_child(self, child, parent):
        for to_check in product(
            child if isinstance(child, list) else [child],
            parent if isinstance(parent, list) else [parent]
        ):
            if is_child_realpath(to_check[0], to_check[1]):
                return True

        return False

    @private
    def is_dataset_path(self, path):
        return path.startswith('/mnt/') and os.stat(path).st_dev != os.stat('/mnt').st_dev

    @filterable_api_method(private=True)
    def mount_info(self, filters, options):
        return filter_list(iter_mountinfo(), filters, options)

    @api_method(FilesystemMkdirArgs, FilesystemMkdirResult, roles=['FILESYSTEM_DATA_WRITE'])
    def mkdir(self, data):
        """
        Create a directory at the specified path.

        The following options are supported:

        `mode` - specify the permissions to set on the new directory (0o755 is default).
        `raise_chmod_error` - choose whether to raise an exception if the attempt to set
        mode fails. In this case, the newly created directory will be removed to prevent
        use with unintended permissions.

        NOTE: if chmod error is skipped, the resulting `mode` key in mkdir response will
        indicate the current permissions on the directory and not the permissions specified
        in the mkdir payload
        """
        path = data['path']
        options = data['options']
        mode = int(options['mode'], 8)

        p = pathlib.Path(path)
        if not p.is_absolute():
            raise CallError(f'{path}: not an absolute path.', errno.EINVAL)

        if p.exists():
            raise CallError(f'{path}: path already exists.', errno.EEXIST)

        realpath = os.path.realpath(path)
        if not realpath.startswith(('/mnt/', '/root/.ssh', '/home/admin/.ssh', f'/home/{TRUENAS_ADMIN_USERNAME}/.ssh')):
            raise CallError(f'{path}: path not permitted', errno.EPERM)

        os.mkdir(path, mode=mode)
        st = stat_x.statx_entry_impl(p)
        stat = st['st']

        if statlib.S_IMODE(stat.stx_mode) != mode:
            # This may happen if requested mode is greater than umask
            # or if underlying dataset has restricted aclmode and ACL is present
            try:
                os.chmod(path, mode)
            except Exception:
                if options['raise_chmod_error']:
                    os.rmdir(path)
                    raise

                self.logger.debug(
                    '%s: failed to set mode %s on path after mkdir call',
                    path, options['mode'], exc_info=True
                )

        return {
            'name': p.parts[-1],
            'path': path,
            'realpath': realpath,
            'type': 'DIRECTORY',
            'size': stat.stx_size,
            'allocation_size': stat.stx_blocks * 512,
            'mode': stat.stx_mode,
            'mtime': stat.stx_mtime,
            'acl': acl_is_present(os.listxattr(path)),
            'uid': stat.stx_uid,
            'gid': stat.stx_gid,
            'is_mountpoint': False,
            'is_ctldir': False,
            'mount_id': st['st'].stx_mnt_id,
            'attributes': st['attributes'],
            'xattrs': [],
            'zfs_attrs': ['ARCHIVE']
        }

    @private
    def listdir_request_mask(self, select):
        """ create request mask for directory listing """
        if not select:
            # request_mask=None means ALL in the directory iterator
            return None

        request_mask = 0
        for i in select:
            # select may be list [key, new_name] to allow
            # equivalent of SELECT AS.
            selected = i[0] if isinstance(i, list) else i

            match selected:
                case 'realpath':
                    request_mask |= DirectoryRequestMask.REALPATH
                case 'acl':
                    request_mask |= DirectoryRequestMask.ACL
                case 'zfs_attrs':
                    request_mask |= DirectoryRequestMask.ZFS_ATTRS
                case 'is_ctldir':
                    request_mask |= DirectoryRequestMask.CTLDIR
                case 'xattrs':
                    request_mask |= DirectoryRequestMask.XATTRS

        return request_mask

    @api_method(FilesystemListdirArgs, FilesystemListdirResult, roles=['FILESYSTEM_ATTRS_READ'])
    def listdir(self, path, filters, options):
        """
        Get the contents of a directory.

        The select option may be used to optimize listdir performance. Metadata-related
        fields that are not selected will not be retrieved from the filesystem.

        For example {"select": ["path", "type"]} will avoid querying an xattr list and
        ZFS attributes for files in a directory.

        """
        path = pathlib.Path(path)
        if not path.exists():
            raise CallError(f'Directory {path} does not exist', errno.ENOENT)

        if not path.is_dir():
            raise CallError(f'Path {path} is not a directory', errno.ENOTDIR)

        if options.get('count') is True:
            # We're just getting count, drop any unnecessary info
            request_mask = 0
        else:
            request_mask = self.listdir_request_mask(options.get('select', None))

        # None request_mask means "everything"
        if request_mask is None or (request_mask & DirectoryRequestMask.ZFS_ATTRS):
            # Make sure this is actually ZFS before issuing FS ioctls
            try:
                self.get_zfs_attributes(str(path))
            except CallError:
                raise
            except Exception:
                raise CallError(f'{path}: ZFS attributes are not supported.')

        file_type = None
        for filter_ in filters:
            if filter_[0] not in ['type']:
                continue

            if filter_[1] != '=':
                continue

            if filter_[2] == 'DIRECTORY':
                file_type = FileType.DIRECTORY
            elif filter_[2] == 'FILE':
                file_type = FileType.FILE
            else:
                continue

        if path.absolute() == pathlib.Path('/mnt'):
            # sometimes (on failures) the top-level directory
            # where the zpool is mounted does not get removed
            # after the zpool is exported. WebUI calls this
            # specifying `/mnt` as the path. This is used when
            # configuring shares in the "Path" drop-down. To
            # prevent shares from being configured to point to
            # a path that doesn't exist on a zpool, we'll
            # filter these here.
            filters.extend([['is_mountpoint', '=', True], ['name', '!=', IX_APPS_DIR_NAME]])

        with DirectoryIterator(path, file_type=file_type, request_mask=request_mask) as d_iter:
            return filter_list(d_iter, filters, options)

    @api_method(FilesystemStatArgs, FilesystemStatResult, roles=['FILESYSTEM_ATTRS_READ'])
    def stat(self, _path):
        """
        Return filesystem information for a given path.

        `realpath(str)`: absolute real path of the entry (if SYMLINK)

        `type(str)`: DIRECTORY | FILE | SYMLINK | OTHER

        `size(int)`: size of the entry

        `allocation_size(int)`: on-disk size of entry

        `mode(int)`: file mode/permission

        `uid(int)`: user id of file owner

        `gid(int)`: group id of file owner

        `atime(float)`: timestamp for when file was last accessed.
        NOTE: this timestamp may be changed from userspace.

        `mtime(float)`: timestamp for when file data was last modified
        NOTE: this timestamp may be changed from userspace.

        `ctime(float)`: timestamp for when file was last changed.

        `btime(float)`: timestamp for when file was initially created.
        NOTE: depending on platform this may be changed from userspace.

        `dev(int)`: device id of the device containing the file. In the
        context of the TrueNAS API, this is sufficient to uniquely identify
        a given dataset.

        `mount_id(int)`: the mount id for the filesystem underlying the given path.
        Bind mounts will have same device id, but different mount IDs. This value
        is sufficient to uniquely identify the particular mount which can be used
        to identify children of the given mountpoint.

        `inode(int)`: inode number of the file. This number uniquely identifies
        the file on the given device, but once a file is deleted its inode number
        may be reused.

        `nlink(int)`: number of hard lnks to the file.

        `acl(bool)`: extended ACL is present on file

        `is_mountpoint(bool)`: path is a mountpoint

        `is_ctldir(bool)`: path is within special .zfs directory

        `attributes(list)`: list of statx file attributes that apply to the
        file. See statx(2) manpage for more details.
        """
        if path_location(_path) is FSLocation.EXTERNAL:
            raise CallError(f'{_path} is external to TrueNAS', errno.EXDEV)

        path = pathlib.Path(_path)
        if not path.is_absolute():
            raise CallError(f'{_path}: path must be absolute', errno.EINVAL)

        st = stat_x.statx_entry_impl(path)
        if st is None:
            raise CallError(f'Path {_path} not found', errno.ENOENT)

        realpath = path.resolve().as_posix() if st['etype'] == 'SYMLINK' else path.absolute().as_posix()

        stat = {
            'realpath': realpath,
            'type': st['etype'],
            'size': st['st'].stx_size,
            'allocation_size': st['st'].stx_blocks * 512,
            'mode': st['st'].stx_mode,
            'uid': st['st'].stx_uid,
            'gid': st['st'].stx_gid,
            'atime': st['st'].stx_atime,
            'mtime': st['st'].stx_mtime,
            'ctime': st['st'].stx_ctime,
            'btime': st['st'].stx_btime,
            'mount_id': st['st'].stx_mnt_id,
            'dev': st['st'].stx_dev,
            'inode': st['st'].stx_ino,
            'nlink': st['st'].stx_nlink,
            'is_mountpoint': 'MOUNT_ROOT' in st['attributes'],
            'is_ctldir': st['is_ctldir'],
            'attributes': st['attributes']
        }

        try:
            stat['user'] = pwd.getpwuid(stat['uid']).pw_name
        except KeyError:
            if stat['uid'] == SYNTHETIC_CONTAINER_ROOT['pw_uid']:
                stat['user'] = SYNTHETIC_CONTAINER_ROOT['pw_name']
            else:
                stat['user'] = None

        try:
            stat['group'] = grp.getgrgid(stat['gid']).gr_name
        except KeyError:
            stat['group'] = None

        stat['acl'] = acl_is_present(os.listxattr(path))

        return stat

    # WARNING: following method cannot currently be audited properly due to RFC limitations on
    # syslog message size.
    @api_method(FilesystemReceiveFileArgs, FilesystemReceiveFileResult, private=True)
    def file_receive(self, path, content, options):
        """
        Simplified file receiving method for small files.

        `content` must be a base 64 encoded file content.
        """
        if path == PWENC_FILE_SECRET:
            raise CallError(
                'Cannot use filesystem.put to write pwenc secret. Use pwenc.replace instead.',
                errno.EINVAL
            )

        dirname = os.path.dirname(path)
        # NOTE: os.makedirs follows symlinks, so an attacker could cause directories
        # to be created at symlink target locations as a side-effect. The subsequent
        # safe_open blocks the actual file write via RESOLVE_NO_SYMLINKS, but the
        # created directories are not rolled back. Fully safe directory creation
        # would require an fd-walking mkdirat implementation.
        os.makedirs(dirname, exist_ok=True)

        with safe_open(path, 'ab' if options.get('append') else 'wb+') as f:
            f.write(binascii.a2b_base64(content))
            if mode := options.get('mode'):
                os.fchmod(f.fileno(), mode)
            # -1 means don't change uid/gid if the one provided is
            # the same that is on disk already
            os.fchown(f.fileno(), options.get('uid', -1), options.get('gid', -1))
            f.flush()

        return True

    @api_method(FilesystemGetArgs, FilesystemGetResult, audit='Filesystem get', roles=['FULL_ADMIN'])
    @job(pipes=["output"])
    def get(self, job, path):
        """
        Job to get contents of `path`.
        """

        if not os.path.isfile(path):
            raise CallError(f'{path} is not a file')

        with safe_open(path, 'rb') as f:
            shutil.copyfileobj(f, job.pipes.output.w)

    @api_method(
        FilesystemGetArchiveArgs, FilesystemGetArchiveResult,
        audit='Filesystem get archive',
        roles=['FILESYSTEM_DATA_READ']
    )
    @job(pipes=["output"])
    def get_archive(self, job, data):
        """
        Job to get contents of multiple files/directories as a ZIP archive.

        `paths` is a list of absolute paths to include in the archive.
        Files are added with their basename; directories are added recursively.
        """
        import zipfile

        paths = data['paths']

        if not paths:
            raise CallError('At least one path is required', errno.EINVAL)

        # Validate all paths exist and are within allowed areas
        for path in paths:
            p = pathlib.Path(path)
            if not p.is_absolute():
                raise CallError(f'{path}: path must be absolute', errno.EINVAL)
            if not p.exists():
                raise CallError(f'{path}: path does not exist', errno.ENOENT)
            realpath = os.path.realpath(path)
            if not realpath.startswith('/mnt/'):
                raise CallError(f'{path}: path not permitted', errno.EPERM)

        # Create ZIP archive and write to output pipe
        with zipfile.ZipFile(job.pipes.output.w, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Track used names to handle duplicates
            used_names = {}

            for path in paths:
                p = pathlib.Path(path)
                base_name = p.name

                # Handle duplicate names by adding suffix
                if base_name in used_names:
                    used_names[base_name] += 1
                    name_parts = base_name.rsplit('.', 1)
                    if len(name_parts) == 2:
                        archive_name = f"{name_parts[0]}_{used_names[base_name]}.{name_parts[1]}"
                    else:
                        archive_name = f"{base_name}_{used_names[base_name]}"
                else:
                    used_names[base_name] = 0
                    archive_name = base_name

                if p.is_file():
                    zf.write(path, archive_name)
                elif p.is_dir():
                    # Add directory recursively
                    for root, dirs, files in os.walk(path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            # Create relative path within archive
                            rel_path = os.path.relpath(file_path, p.parent)
                            zf.write(file_path, rel_path)

    @api_method(FilesystemPutArgs, FilesystemPutResult, audit='Filesystem put', roles=['FULL_ADMIN'])
    @job(pipes=["input"])
    def put(self, job, path, options):
        """
        Job to put contents to `path`.
        """
        if path == PWENC_FILE_SECRET:
            raise CallError(
                'Cannot use filesystem.put to write pwenc secret. Use pwenc.replace instead.',
                errno.EINVAL
            )

        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            # NOTE: os.makedirs follows symlinks, so an attacker could cause directories
            # to be created at symlink target locations as a side-effect. The subsequent
            # safe_open blocks the actual file write via RESOLVE_NO_SYMLINKS, but the
            # created directories are not rolled back. Fully safe directory creation
            # would require an fd-walking mkdirat implementation.
            os.makedirs(dirname)
        if options.get('append'):
            openmode = 'ab'
        else:
            openmode = 'wb+'

        mode = options.get('mode')

        try:
            with safe_open(path, openmode) as f:
                if mode:
                    os.fchmod(f.fileno(), mode)

                shutil.copyfileobj(job.pipes.input.r, f)
        except PermissionError:
            raise CallError(f'Unable to put contents at {path!r} as the path exists on a locked dataset', errno.EINVAL)

        return True

    @api_method(FilesystemStatfsArgs, FilesystemStatfsResult, roles=['FILESYSTEM_ATTRS_READ'])
    def statfs(self, path):
        """
        Return stats from the filesystem of a given path.

        Raises:
            CallError(ENOENT) - Path not found
        """
        try:
            fd = truenas_os.openat2(path, os.O_PATH, resolve=truenas_os.RESOLVE_NO_SYMLINKS)
            try:
                st = os.fstatvfs(fd)
                mntinfo = statmount(fd=fd)
            finally:
                os.close(fd)

        except FileNotFoundError:
            raise CallError('Path not found.', errno.ENOENT)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise CallError('Symlinks are not permitted.', errno.ELOOP)
            raise

        flags = mntinfo['mount_opts']
        for flag in mntinfo['super_opts']:
            if flag in flags:
                continue
            flags.append(flag)

        result = {
            'flags': flags,
            'fstype': mntinfo['fs_type'].lower(),
            'source': mntinfo['mount_source'],
            'dest': mntinfo['mountpoint'],
            'blocksize': st.f_frsize,
            'total_blocks': st.f_blocks,
            'free_blocks': st.f_bfree,
            'avail_blocks': st.f_bavail,
            'files': st.f_files,
            'free_files': st.f_ffree,
            'name_max': st.f_namemax,
            'fsid': str(st.f_fsid),
            'total_bytes': st.f_blocks * st.f_frsize,
            'free_bytes': st.f_bfree * st.f_frsize,
            'avail_bytes': st.f_bavail * st.f_frsize,
        }
        for k in ['total_blocks', 'free_blocks', 'avail_blocks', 'total_bytes', 'free_bytes', 'avail_bytes']:
            result[f'{k}_str'] = str(result[k])
        return result

    @api_method(
        FilesystemRenameArgs, FilesystemRenameResult,
        roles=['FILESYSTEM_DATA_WRITE'],
        audit='Filesystem rename',
        audit_extended=lambda data: f"{data['src']} -> {data['dst']}"
    )
    def rename(self, data):
        """
        Rename or move a file or directory from `src` to `dst`.

        This is equivalent to the POSIX rename() system call.
        If the destination exists and is a file, it will be replaced.
        If the destination exists and is a directory, an error will be raised.
        """
        src = pathlib.Path(data['src'])
        dst = pathlib.Path(data['dst'])

        if not src.is_absolute():
            raise CallError(f'{data["src"]}: source path must be absolute', errno.EINVAL)

        if not dst.is_absolute():
            raise CallError(f'{data["dst"]}: destination path must be absolute', errno.EINVAL)

        if not src.exists():
            raise CallError(f'{data["src"]}: source path does not exist', errno.ENOENT)

        # Validate paths are within allowed areas
        src_realpath = os.path.realpath(data['src'])
        dst_realpath = os.path.realpath(os.path.dirname(data['dst']))
        for path in [src_realpath, dst_realpath]:
            if not path.startswith('/mnt/'):
                raise CallError(f'{path}: path not permitted', errno.EPERM)

        try:
            os.rename(data['src'], data['dst'])
        except OSError as e:
            raise CallError(f'Failed to rename {data["src"]} to {data["dst"]}: {e.strerror}', e.errno)

        return True

    @api_method(
        FilesystemCopyArgs, FilesystemCopyResult,
        roles=['FILESYSTEM_DATA_WRITE'],
        audit='Filesystem copy',
        audit_extended=lambda data: f"{data['src']} -> {data['dst']}"
    )
    @job(lock=lambda args: f'filesystem_copy_{args[0]["src"]}')
    def copy(self, job, data):
        """
        Copy a file or directory from `src` to `dst`.

        If `src` is a directory and `options.recursive` is True (default),
        the entire directory tree will be copied.

        If `options.preserve_attrs` is True, file attributes (mode, timestamps)
        will be preserved.
        """
        src = pathlib.Path(data['src'])
        dst = pathlib.Path(data['dst'])
        options = data.get('options', {})

        if not src.is_absolute():
            raise CallError(f'{data["src"]}: source path must be absolute', errno.EINVAL)

        if not dst.is_absolute():
            raise CallError(f'{data["dst"]}: destination path must be absolute', errno.EINVAL)

        if not src.exists():
            raise CallError(f'{data["src"]}: source path does not exist', errno.ENOENT)

        # Validate paths are within allowed areas
        src_realpath = os.path.realpath(data['src'])
        dst_parent = os.path.dirname(data['dst'])
        if dst_parent:
            dst_realpath = os.path.realpath(dst_parent)
        else:
            dst_realpath = os.path.realpath(data['dst'])

        for path in [src_realpath, dst_realpath]:
            if not path.startswith('/mnt/'):
                raise CallError(f'{path}: path not permitted', errno.EPERM)

        try:
            if src.is_dir():
                if not options.get('recursive', True):
                    raise CallError(f'{data["src"]} is a directory. Use recursive option to copy directories.')

                if options.get('preserve_attrs', False):
                    shutil.copytree(data['src'], data['dst'], copy_function=shutil.copy2)
                else:
                    shutil.copytree(data['src'], data['dst'])
            else:
                if options.get('preserve_attrs', False):
                    shutil.copy2(data['src'], data['dst'])
                else:
                    shutil.copy(data['src'], data['dst'])
        except OSError as e:
            raise CallError(f'Failed to copy {data["src"]} to {data["dst"]}: {e.strerror}', e.errno)
        except shutil.Error as e:
            raise CallError(f'Failed to copy {data["src"]} to {data["dst"]}: {str(e)}')

        return True

    @api_method(
        FilesystemMoveArgs, FilesystemMoveResult,
        roles=['FILESYSTEM_DATA_WRITE'],
        audit='Filesystem move',
        audit_extended=lambda data: f"{data['src']} -> {data['dst']}"
    )
    @job(lock=lambda args: f'filesystem_move_{args[0]["dst"]}')
    def move(self, job, data):
        """
        Move files or directories to a destination directory.

        `src` is a list of source paths to move.
        `dst` is the destination directory path.

        This method automatically handles both same-filesystem moves (using rename)
        and cross-filesystem moves (using copy + delete).

        For cross-filesystem moves, file attributes and timestamps are preserved.
        """
        sources = data['src']
        dst_dir = pathlib.Path(data['dst'])
        options = data.get('options', {})

        if not sources:
            raise CallError('At least one source path is required', errno.EINVAL)

        if not dst_dir.is_absolute():
            raise CallError(f'{data["dst"]}: destination path must be absolute', errno.EINVAL)

        if not dst_dir.exists():
            raise CallError(f'{data["dst"]}: destination directory does not exist', errno.ENOENT)

        if not dst_dir.is_dir():
            raise CallError(f'{data["dst"]}: destination must be a directory', errno.ENOTDIR)

        # Validate destination is within allowed areas
        dst_realpath = os.path.realpath(data['dst'])
        if not dst_realpath.startswith('/mnt/'):
            raise CallError(f'{data["dst"]}: path not permitted', errno.EPERM)

        # Get destination mount_id for comparison
        try:
            dst_stat = stat_x.statx_entry_impl(dst_dir)
            dst_mount_id = dst_stat['st'].stx_mnt_id
        except Exception as e:
            raise CallError(f'Failed to stat destination: {e}', errno.EIO)

        total = len(sources)
        moved = 0

        for src_path in sources:
            src = pathlib.Path(src_path)

            if not src.is_absolute():
                raise CallError(f'{src_path}: source path must be absolute', errno.EINVAL)

            if not src.exists():
                raise CallError(f'{src_path}: source path does not exist', errno.ENOENT)

            # Validate source is within allowed areas
            src_realpath = os.path.realpath(src_path)
            if not src_realpath.startswith('/mnt/'):
                raise CallError(f'{src_path}: path not permitted', errno.EPERM)

            # Prevent moving mount points
            if src.is_mount():
                raise CallError(f'{src_path}: cannot move a mount point', errno.EPERM)

            # Determine destination file path
            dest_path = dst_dir / src.name

            # Check if source and destination are on the same filesystem
            try:
                src_stat = stat_x.statx_entry_impl(src)
                src_mount_id = src_stat['st'].stx_mnt_id
            except Exception as e:
                raise CallError(f'Failed to stat source: {e}', errno.EIO)

            same_fs = (src_mount_id == dst_mount_id)

            try:
                if same_fs:
                    # Same filesystem - use rename (fast, atomic)
                    os.rename(src_path, str(dest_path))
                else:
                    # Cross filesystem - copy then delete
                    if src.is_dir():
                        if not options.get('recursive', True):
                            raise CallError(f'{src_path} is a directory. Use recursive option.')
                        shutil.copytree(src_path, str(dest_path), copy_function=shutil.copy2)
                        shutil.rmtree(src_path)
                    else:
                        shutil.copy2(src_path, str(dest_path))
                        os.unlink(src_path)
            except OSError as e:
                raise CallError(f'Failed to move {src_path} to {dest_path}: {e.strerror}', e.errno)
            except shutil.Error as e:
                raise CallError(f'Failed to move {src_path} to {dest_path}: {str(e)}')

            moved += 1
            job.set_progress(int((moved / total) * 100), f'Moved {moved}/{total} items')

        return True

    @api_method(
        FilesystemDeleteArgs, FilesystemDeleteResult,
        roles=['FILESYSTEM_DATA_WRITE'],
        audit='Filesystem delete',
        audit_extended=lambda data: data['path']
    )
    def delete(self, data):
        """
        Delete a file or directory at the specified path.

        If the path is a directory and `options.recursive` is True,
        the entire directory tree will be deleted.

        If the path is a directory and `options.recursive` is False (default),
        an error will be raised if the directory is not empty.
        """
        path = pathlib.Path(data['path'])
        options = data.get('options', {})

        if not path.is_absolute():
            raise CallError(f'{data["path"]}: path must be absolute', errno.EINVAL)

        if not path.exists():
            raise CallError(f'{data["path"]}: path does not exist', errno.ENOENT)

        # Validate path is within allowed areas
        realpath = os.path.realpath(data['path'])
        if not realpath.startswith('/mnt/'):
            raise CallError(f'{data["path"]}: path not permitted', errno.EPERM)

        # Prevent deleting mount points
        if path.is_mount():
            raise CallError(f'{data["path"]}: cannot delete a mount point', errno.EPERM)

        try:
            if path.is_dir():
                if options.get('recursive', False):
                    shutil.rmtree(data['path'])
                else:
                    os.rmdir(data['path'])
            else:
                os.unlink(data['path'])
        except OSError as e:
            raise CallError(f'Failed to delete {data["path"]}: {e.strerror}', e.errno)

        return True
