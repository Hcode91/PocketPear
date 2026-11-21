"""The device capability table SpringBoard publishes as the "GSCapabilities" shared memory object.

GraphicsServices (GSSystemHasCapability, GSSystemCopyCapability, the main screen metrics) reads
a length-prefixed XML property list from it, retrying for half a second each time it is missing, so without it
every capability question costs half a second and answers "no". These are an iPhone 5's."""
import plistlib

IPHONE5 = {
    'DeviceClassNumber': 1,
    'device-name': 'iPhone',
    'device-name-localized': 'iPhone',
    'marketing-name': 'iPhone 5',
    'main-screen-width': 640,
    'main-screen-height': 1136,
    'main-screen-scale': 2.0,
    'main-screen-orientation': 0.0,
    'screen-dimensions': {'main-screen-width': 640, 'main-screen-height': 1136,
                          'main-screen-scale': 2.0, 'main-screen-orientation': 0.0},
    'armv6': True, 'armv7': True, 'armv7s': True,
    'opengles-1': True, 'opengles-2': True,
    'accelerometer': True, 'gyroscope': True, 'magnetometer': True,
    'ambient-light-sensor': True, 'proximity-sensor': True,
    'still-camera': True, 'video-camera': True, 'auto-focus-camera': True, 'camera-flash': True,
    'front-facing-camera': True, 'rear-facing-camera': True, 'hd-video-capture': True,
    'hdr-image-capture': True,
    'microphone': True, 'wifi': True, 'bluetooth': True, 'bluetooth-le': True, 'peer-peer': True,
    'gps': True, 'location-services': True, 'telephony': True, 'any-telephony': True,
    'contains-cellular-radio': True, 'cellular-data': True, 'sms': True, 'lte-device': True,
    'multitasking': True, 'multitasking-gestures': False, 'ringer-switch': True,
    'volume-buttons': True, 'piezo-clicker': False, 'display-mirroring': True,
    'io-surface-backed-images': True, 'h264-encoder': True, 'encode-aac': True,
    'international-settings': True, 'photo-stream': True, 'location-reminders': True,
    'personal-hotspot': True, 'siri-gesture': True, 'assistant': True,
    'hardware-keyboard': False, 'apple-internal-install': False,
}


def plist_bytes():
    """The object's contents: a 32-bit length, then that many bytes of XML property list."""
    import struct
    xml = plistlib.dumps(IPHONE5, fmt=plistlib.FMT_XML)
    return struct.pack('<I', len(xml)) + xml
