# Minimal imghdr shim for environments where imghdr isn't available.
# It implements the imghdr.what() function for a few common formats.

import struct

def what(filename, h=None):
    """Return a guess for the image type based on the data in the file or
    the provided header bytes.
    """
    if h is None:
        try:
            with open(filename, 'rb') as f:
                h = f.read(32)
        except Exception:
            return None
    if len(h) >= 10 and h.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if h.startswith(b'GIF87a') or h.startswith(b'GIF89a'):
        return 'gif'
    if len(h) >= 3 and h[0:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if h.startswith(b'BM'):
        return 'bmp'
    if h.startswith(b'RIFF') and h[8:12] == b'WEBP':
        return 'webp'
    return None
