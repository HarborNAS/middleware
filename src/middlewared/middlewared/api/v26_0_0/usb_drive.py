from typing import Literal

from middlewared.api.base import BaseModel, single_argument_args


__all__ = [
    "UsbDriveEntry",
    "UsbDriveQueryArgs",
    "UsbDriveQueryResult",
    "UsbDriveMountArgs",
    "UsbDriveMountResult",
    "UsbDriveUnmountArgs",
    "UsbDriveUnmountResult",
    "UsbDriveEjectArgs",
    "UsbDriveEjectResult",
    "UsbDriveQueryAddedEvent",
    "UsbDriveQueryChangedEvent",
    "UsbDriveQueryRemovedEvent",
]


class UsbDriveEntry(BaseModel):
    id: str
    """Unique identifier for the USB drive (device name like 'sda')."""
    name: str
    """Device name (e.g., 'sda')."""
    serial: str | None
    """Serial number of the USB drive."""
    vendor: str | None
    """Vendor name of the USB drive."""
    model: str | None
    """Model name of the USB drive."""
    size: int | None
    """Size of the USB drive in bytes."""
    mountpoint: str | None
    """Current mount point path, or null if not mounted."""
    fstype: str | None
    """Filesystem type (e.g., 'vfat', 'ntfs', 'ext4')."""
    label: str | None
    """Filesystem label if available."""
    readonly: bool | None = False
    """Whether the drive or partition is mounted read-only."""
    bus: int | None
    """USB bus number."""
    dev: int | None
    """USB device number."""
    partitions: list[dict] | None
    """List of partitions on the drive."""


class UsbDriveQueryArgs(BaseModel):
    query_filters: list = []
    query_options: dict = {}


class UsbDriveQueryResult(BaseModel):
    result: list[UsbDriveEntry]


@single_argument_args("usb_drive_mount")
class UsbDriveMountArgs(BaseModel):
    id: str
    """Device name of the USB drive to mount (e.g., 'sda1')."""
    options: dict = {}
    """Optional mount options."""


class UsbDriveMountResult(BaseModel):
    result: str
    """Mount point path where the USB drive was mounted."""


@single_argument_args("usb_drive_unmount")
class UsbDriveUnmountArgs(BaseModel):
    id: str
    """Device name of the USB drive to unmount (e.g., 'sda1')."""
    force: bool = False
    """Force unmount even if the device is busy."""


class UsbDriveUnmountResult(BaseModel):
    result: Literal[True]


@single_argument_args("usb_drive_eject")
class UsbDriveEjectArgs(BaseModel):
    id: str
    """Device name of the USB drive to eject (e.g., 'sda')."""


class UsbDriveEjectResult(BaseModel):
    result: Literal[True]


class UsbDriveQueryAddedEvent(BaseModel):
    id: str
    fields: UsbDriveEntry


class UsbDriveQueryChangedEvent(BaseModel):
    id: str
    fields: UsbDriveEntry


class UsbDriveQueryRemovedEvent(BaseModel):
    id: str
