import errno
import os
import subprocess

import pyudev

from middlewared.api import api_method, Event
from middlewared.api.current import (
    UsbDriveEntry,
    UsbDriveMountArgs,
    UsbDriveMountResult,
    UsbDriveUnmountArgs,
    UsbDriveUnmountResult,
    UsbDriveEjectArgs,
    UsbDriveEjectResult,
    UsbDriveQueryAddedEvent,
    UsbDriveQueryChangedEvent,
    UsbDriveQueryRemovedEvent,
)
from middlewared.service import Service, private, filterable_api_method, CallError
from middlewared.utils.mount import getmntinfo

USB_MOUNT_BASE = '/mnt/.usb'


class UsbDriveService(Service):

    class Config:
        namespace = 'usb.drive'
        cli_namespace = 'storage.usb.drive'
        role_prefix = 'FILESYSTEM_DATA'
        entry = UsbDriveEntry
        event_register = False
        event_send = False
        events = [
            Event(
                name='usb.drive.query',
                description='Sent on USB drive changes (insert/remove/mount/unmount).',
                roles=['READONLY_ADMIN'],
                models={
                    'ADDED': UsbDriveQueryAddedEvent,
                    'CHANGED': UsbDriveQueryChangedEvent,
                    'REMOVED': UsbDriveQueryRemovedEvent,
                }
            )
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usb_drives = {}

    @filterable_api_method(item=UsbDriveEntry)
    async def query(self, filters, options):
        """
        Query currently connected USB storage drives.

        Returns a list of USB drives with their mount status.
        """
        drives = await self.middleware.run_in_thread(self._get_usb_drives)
        return list(drives.values())

    @private
    def _get_usb_drives(self):
        """Get all USB storage devices."""
        drives = {}
        context = pyudev.Context()

        # Get mount info for checking mounted status
        try:
            mntinfo = getmntinfo()
        except Exception:
            mntinfo = {}

        mount_by_device = {}
        for mnt in mntinfo.values():
            src = mnt.get('mount_source', '')
            if src.startswith('/dev/'):
                dev_name = os.path.basename(src)
                mount_by_device[dev_name] = mnt

        for device in context.list_devices(subsystem='block'):
            # Check if this is a USB device
            if not self._is_usb_storage(device):
                continue

            dev_name = device.sys_name
            dev_path = device.device_node

            if not dev_path:
                continue

            # Get device properties
            props = device.properties
            size = props.get('ID_PART_ENTRY_SIZE')
            if size:
                size = int(size) * 512  # Convert sectors to bytes
            else:
                # Try to get size from sysfs
                size_path = f'/sys/block/{dev_name.rstrip("0123456789")}/{dev_name}/size'
                if not os.path.exists(size_path):
                    size_path = f'/sys/block/{dev_name}/size'
                try:
                    if os.path.exists(size_path):
                        with open(size_path) as f:
                            size = int(f.read().strip()) * 512
                except Exception:
                    size = None

            # Get mount info
            mnt = mount_by_device.get(dev_name)
            mountpoint = mnt.get('mountpoint') if mnt else None
            fstype = mnt.get('fs_type') if mnt else props.get('ID_FS_TYPE')

            # Get USB bus/dev info
            bus = None
            dev_num = None
            usb_device = device.find_parent('usb', 'usb_device')
            if usb_device:
                busnum = usb_device.attributes.get('busnum')
                devnum = usb_device.attributes.get('devnum')
                if busnum:
                    bus = int(busnum)
                if devnum:
                    dev_num = int(devnum)

            # Get partitions if this is a whole disk
            partitions = None
            if device.device_type == 'disk':
                partitions = []
                for part in context.list_devices(subsystem='block', DEVTYPE='partition', parent=device):
                    part_mnt = mount_by_device.get(part.sys_name)
                    part_props = part.properties
                    partitions.append({
                        'name': part.sys_name,
                        'size': int(part_props.get('ID_PART_ENTRY_SIZE', 0)) * 512 if part_props.get('ID_PART_ENTRY_SIZE') else None,
                        'fstype': part_mnt.get('fs_type') if part_mnt else part_props.get('ID_FS_TYPE'),
                        'label': part_props.get('ID_FS_LABEL'),
                        'mountpoint': part_mnt.get('mountpoint') if part_mnt else None,
                    })

            drives[dev_name] = {
                'id': dev_name,
                'name': dev_name,
                'serial': props.get('ID_SERIAL_SHORT') or props.get('ID_SERIAL'),
                'vendor': props.get('ID_VENDOR') or props.get('ID_USB_VENDOR'),
                'model': props.get('ID_MODEL') or props.get('ID_USB_MODEL'),
                'size': size,
                'mountpoint': mountpoint,
                'fstype': fstype,
                'label': props.get('ID_FS_LABEL'),
                'bus': bus,
                'dev': dev_num,
                'partitions': partitions,
            }

        return drives

    @private
    def _is_usb_storage(self, device):
        """Check if a block device is a USB storage device."""
        # Only return whole disks, not partitions (partitions are in the disk's partitions array)
        if device.device_type != 'disk':
            return False

        # Check for USB bus
        props = device.properties
        if props.get('ID_BUS') == 'usb':
            return True

        # Check parent devices for USB
        parent = device.find_parent('usb', 'usb_device')
        if parent:
            return True

        # Check device path
        if props.get('ID_PATH', '').startswith('pci-') and '-usb-' in props.get('ID_PATH', ''):
            return True

        return False

    @api_method(UsbDriveMountArgs, UsbDriveMountResult, roles=['FILESYSTEM_DATA_WRITE'])
    async def mount(self, data):
        """
        Mount a USB drive partition.

        `id` - Device name of the partition to mount (e.g., 'sda1').

        Returns the mount point path.
        """
        dev_name = data['id']
        dev_path = f'/dev/{dev_name}'

        if not os.path.exists(dev_path):
            raise CallError(f'Device {dev_path} does not exist', errno.ENOENT)

        # Check if already mounted
        drives = await self.middleware.run_in_thread(self._get_usb_drives)

        # dev_name could be a partition (e.g., sda1) or a whole disk (e.g., sda)
        # _get_usb_drives only returns whole disks, so we need to find the partition info
        base_name = dev_name.rstrip('0123456789')
        if base_name not in drives:
            raise CallError(f'{dev_name} is not a USB storage device', errno.EINVAL)

        base_drive = drives[base_name]

        # Find the partition or drive info
        drive = None
        if dev_name == base_name:
            # Mounting whole disk (no partitions)
            drive = base_drive
        else:
            # Mounting a partition
            for part in (base_drive.get('partitions') or []):
                if part['name'] == dev_name:
                    drive = part
                    break
            if not drive:
                raise CallError(f'{dev_name} is not a valid partition', errno.EINVAL)

        if drive.get('mountpoint'):
            return drive['mountpoint']

        # Create mount point
        mountpoint = await self._get_mount_point(dev_name, drive.get('label'))
        os.makedirs(mountpoint, exist_ok=True)

        # Determine filesystem type
        fstype = drive.get('fstype')
        mount_opts = ['rw']

        if fstype == 'vfat':
            mount_opts.extend(['uid=0', 'gid=0', 'umask=0022'])
        elif fstype == 'ntfs':
            # Use ntfs-3g for better NTFS support
            fstype = 'ntfs-3g'
            mount_opts.extend(['uid=0', 'gid=0', 'umask=0022'])
        elif fstype in ('ext2', 'ext3', 'ext4', 'xfs'):
            pass  # Default options are fine
        elif fstype == 'exfat':
            mount_opts.extend(['uid=0', 'gid=0', 'umask=0022'])

        try:
            cmd = ['mount']
            if fstype:
                cmd.extend(['-t', fstype])
            if mount_opts:
                cmd.extend(['-o', ','.join(mount_opts)])
            cmd.extend([dev_path, mountpoint])

            await self.middleware.run_in_thread(
                lambda: subprocess.run(cmd, check=True, capture_output=True, text=True)
            )
        except subprocess.CalledProcessError as e:
            # Cleanup mount point on failure
            try:
                os.rmdir(mountpoint)
            except Exception:
                pass
            raise CallError(f'Failed to mount {dev_path}: {e.stderr}', errno.EIO)

        # Send event
        updated_drive = (await self.middleware.run_in_thread(self._get_usb_drives)).get(dev_name)
        if updated_drive:
            self.middleware.send_event('usb.drive.query', 'CHANGED', id=dev_name, fields=updated_drive)

        return mountpoint

    @private
    async def _get_mount_point(self, dev_name, label=None):
        """Generate a mount point path for a USB drive."""
        if label:
            # Sanitize label for use as directory name
            safe_label = ''.join(c if c.isalnum() or c in '-_' else '_' for c in label)
            return f'{USB_MOUNT_BASE}/{safe_label}'
        return f'{USB_MOUNT_BASE}/{dev_name}'

    @api_method(UsbDriveUnmountArgs, UsbDriveUnmountResult, roles=['FILESYSTEM_DATA_WRITE'])
    async def unmount(self, data):
        """
        Unmount a USB drive partition.

        `id` - Device name of the partition to unmount (e.g., 'sda1').
        `force` - Force unmount even if the device is busy.
        """
        dev_name = data['id']

        drives = await self.middleware.run_in_thread(self._get_usb_drives)

        # dev_name could be a partition (e.g., sda1) or a whole disk (e.g., sda)
        base_name = dev_name.rstrip('0123456789')
        if base_name not in drives:
            raise CallError(f'{dev_name} is not a USB storage device', errno.EINVAL)

        base_drive = drives[base_name]

        # Find the partition or drive info
        drive = None
        if dev_name == base_name:
            drive = base_drive
        else:
            for part in (base_drive.get('partitions') or []):
                if part['name'] == dev_name:
                    drive = part
                    break
            if not drive:
                raise CallError(f'{dev_name} is not a valid partition', errno.EINVAL)

        if not drive.get('mountpoint'):
            # Already unmounted
            return True

        mountpoint = drive['mountpoint']

        try:
            cmd = ['umount']
            if data.get('force'):
                cmd.append('-f')
            cmd.append(mountpoint)

            await self.middleware.run_in_thread(
                lambda: subprocess.run(cmd, check=True, capture_output=True, text=True)
            )
        except subprocess.CalledProcessError as e:
            raise CallError(f'Failed to unmount {mountpoint}: {e.stderr}', errno.EBUSY)

        # Remove mount point directory if it's under our USB mount base
        if mountpoint.startswith(USB_MOUNT_BASE):
            try:
                os.rmdir(mountpoint)
            except Exception:
                pass

        # Send event
        updated_drive = (await self.middleware.run_in_thread(self._get_usb_drives)).get(dev_name)
        if updated_drive:
            self.middleware.send_event('usb.drive.query', 'CHANGED', id=dev_name, fields=updated_drive)

        return True

    @api_method(UsbDriveEjectArgs, UsbDriveEjectResult, roles=['FILESYSTEM_DATA_WRITE'])
    async def eject(self, data):
        """
        Safely eject a USB drive.

        This unmounts all partitions and prepares the drive for safe removal.

        `id` - Device name of the USB drive to eject (e.g., 'sda').
        """
        dev_name = data['id']

        drives = await self.middleware.run_in_thread(self._get_usb_drives)

        # Find the base device (remove partition numbers)
        base_name = dev_name.rstrip('0123456789')
        if base_name not in drives:
            raise CallError(f'{base_name} is not a USB storage device', errno.EINVAL)

        # Unmount all partitions
        for name, drive in drives.items():
            if name.startswith(base_name) and drive['mountpoint']:
                await self.unmount({'id': name, 'force': False})

        # Sync filesystems
        await self.middleware.run_in_thread(lambda: subprocess.run(['sync'], check=True))

        # Use udisksctl or eject to safely remove
        dev_path = f'/dev/{base_name}'
        try:
            # Try eject command first
            await self.middleware.run_in_thread(
                lambda: subprocess.run(['eject', dev_path], check=True, capture_output=True, text=True)
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback: flush and power off the device
            try:
                # Flush device buffers
                await self.middleware.run_in_thread(
                    lambda: subprocess.run(['blockdev', '--flushbufs', dev_path], check=True, capture_output=True)
                )
            except Exception:
                pass

        # Send removed event
        self.middleware.send_event('usb.drive.query', 'REMOVED', id=base_name)

        return True

    @private
    async def auto_mount(self, dev_name):
        """Auto-mount a USB drive partition when inserted."""
        drives = await self.middleware.run_in_thread(self._get_usb_drives)
        if dev_name not in drives:
            return

        drive = drives[dev_name]

        # Only auto-mount partitions, not whole disks
        if drive['partitions'] is not None:
            # This is a whole disk, mount its partitions instead
            for part in drive['partitions']:
                if part.get('fstype') and not part.get('mountpoint'):
                    try:
                        await self.mount({'id': part['name']})
                    except Exception as e:
                        self.logger.warning(f'Failed to auto-mount {part["name"]}: {e}')
        elif drive.get('fstype') and not drive.get('mountpoint'):
            # This is a partition
            try:
                await self.mount({'id': dev_name})
            except Exception as e:
                self.logger.warning(f'Failed to auto-mount {dev_name}: {e}')

    @private
    async def auto_unmount(self, dev_name):
        """Auto-unmount a USB drive when removed."""
        # The device is already gone, we just need to clean up stale mounts
        try:
            mntinfo = await self.middleware.run_in_thread(getmntinfo)
        except Exception:
            mntinfo = {}

        # Also check /mnt/.usb for any leftover mount directories that might be stale
        mountpoints_to_cleanup = set()

        # Find mounts by device name
        for mnt in mntinfo.values():
            src = mnt.get('mount_source', '')
            if src.startswith(f'/dev/{dev_name}'):
                mountpoint = mnt.get('mountpoint')
                if mountpoint and mountpoint.startswith(USB_MOUNT_BASE):
                    mountpoints_to_cleanup.add(mountpoint)

        # Unmount each mount point with force and lazy flags
        # Use both -f (force) and -l (lazy) to handle stuck mounts
        for mountpoint in mountpoints_to_cleanup:
            try:
                # First try lazy unmount which always succeeds even if device is gone
                await self.middleware.run_in_thread(
                    lambda mp=mountpoint: subprocess.run(
                        ['umount', '-l', '-f', mp], capture_output=True
                    )
                )
            except Exception:
                pass

        # Wait a moment for unmounts to complete
        await self.middleware.run_in_thread(lambda: __import__('time').sleep(0.2))

        # Clean up mount point directories
        for mountpoint in mountpoints_to_cleanup:
            try:
                # Check if it's still mounted (shouldn't be after lazy unmount)
                mntinfo_after = await self.middleware.run_in_thread(getmntinfo)
                still_mounted = any(
                    m.get('mountpoint') == mountpoint for m in mntinfo_after.values()
                )
                if not still_mounted:
                    # Try to remove the directory
                    import shutil
                    if os.path.isdir(mountpoint):
                        # If directory is empty, use rmdir, otherwise use rmtree
                        try:
                            os.rmdir(mountpoint)
                        except OSError:
                            # Directory might not be empty due to stale files
                            shutil.rmtree(mountpoint, ignore_errors=True)
            except Exception:
                pass


async def mount_existing_usb_drives(middleware):
    """
    Scan for existing USB drives and mount them.
    This is called on startup to handle drives present at boot.
    """
    # Wait for system to settle
    await middleware.run_in_thread(lambda: __import__('time').sleep(5.0))

    try:
        drives = await middleware.call('usb.drive._get_usb_drives')
        for dev_name in drives:
            try:
                await middleware.call('usb.drive.auto_mount', dev_name)
            except Exception:
                pass
    except Exception:
        pass


async def udev_usb_storage_hook(middleware, data):
    """Handle USB storage device events from udev."""
    if data.get('SUBSYSTEM') != 'block':
        return

    # Check if this is a USB device
    if data.get('ID_BUS') != 'usb' and '-usb-' not in data.get('ID_PATH', ''):
        return

    dev_name = data.get('SYS_NAME')
    if not dev_name:
        return

    # Handle partitions and disks differently
    devtype = data.get('DEVTYPE')
    action = data.get('ACTION')

    if devtype == 'partition':
        # When a partition appears/disappears, re-query and send a CHANGED event for the parent disk
        # This ensures the frontend sees updated partition/mount info promptly
        base_name = dev_name.rstrip('0123456789')
        if action == 'add':
            # Brief delay for partition to settle
            await middleware.run_in_thread(lambda: __import__('time').sleep(0.3))
            drives = await middleware.run_in_thread(
                lambda: middleware.call_sync('usb.drive._get_usb_drives')
            )
            if base_name in drives:
                middleware.send_event('usb.drive.query', 'CHANGED', id=base_name, fields=drives[base_name])
                # Auto-mount this partition
                await middleware.call('usb.drive.auto_mount', base_name)
        elif action == 'remove':
            # Partition was removed - update the parent disk info
            drives = await middleware.run_in_thread(
                lambda: middleware.call_sync('usb.drive._get_usb_drives')
            )
            if base_name in drives:
                middleware.send_event('usb.drive.query', 'CHANGED', id=base_name, fields=drives[base_name])
        return

    # Handle whole disk events
    if action == 'add':
        # Delay to let the device settle and partitions to be detected
        await middleware.run_in_thread(lambda: __import__('time').sleep(1.0))

        # Get updated drive info
        drives = await middleware.run_in_thread(
            lambda: middleware.call_sync('usb.drive._get_usb_drives')
        )
        if dev_name in drives:
            middleware.send_event('usb.drive.query', 'ADDED', id=dev_name, fields=drives[dev_name])
            # Auto-mount partitions
            await middleware.call('usb.drive.auto_mount', dev_name)

    elif action == 'remove':
        # Auto-unmount and cleanup
        await middleware.call('usb.drive.auto_unmount', dev_name)
        middleware.send_event('usb.drive.query', 'REMOVED', id=dev_name)


def setup(middleware):
    # Create USB mount base directory
    os.makedirs(USB_MOUNT_BASE, exist_ok=True)

    # Register udev hook for USB storage events
    middleware.register_hook('udev.block', udev_usb_storage_hook)

    # Auto-mount existing USB drives
    middleware.create_task(mount_existing_usb_drives(middleware))
