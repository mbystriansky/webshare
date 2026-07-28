# MD5-based password crypt implementation (BSD-style $1$ prefix)
# Compatible with passlib.hash.md5_crypt
#
# The $1$ scheme was designed by Poul-Henning Kamp for FreeBSD
# (crypt-md5.c, "beer-ware" license). This is an independent Python
# port of the published algorithm; see the project LICENSE file.

import hashlib


MAGIC = b'$1$'
ITOA64 = b'./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'


def _to64(v, n):
    result = b''
    while n > 0:
        result += bytes([ITOA64[v & 0x3f]])
        v >>= 6
        n -= 1
    return result


def md5crypt(password, salt):
    if isinstance(password, str):
        password = password.encode('utf-8')
    if isinstance(salt, str):
        salt = salt.encode('utf-8')

    # Remove $1$ prefix from salt if present
    if salt.startswith(MAGIC):
        salt = salt[len(MAGIC):]

    # Salt ends at '$' (a full crypt string carries the hash after it),
    # and is at most 8 characters
    salt = salt.split(b'$', 1)[0][:8]

    ctx = hashlib.md5(password + MAGIC + salt)

    alt = hashlib.md5(password + salt + password).digest()

    plen = len(password)
    i = plen
    while i > 0:
        ctx.update(alt[:min(16, i)])
        i -= 16

    i = plen
    while i:
        if i & 1:
            ctx.update(b'\x00')
        else:
            ctx.update(password[:1])
        i >>= 1

    result = ctx.digest()

    for i in range(1000):
        ctx1 = hashlib.md5()
        if i & 1:
            ctx1.update(password)
        else:
            ctx1.update(result)
        if i % 3:
            ctx1.update(salt)
        if i % 7:
            ctx1.update(password)
        if i & 1:
            ctx1.update(result)
        else:
            ctx1.update(password)
        result = ctx1.digest()

    output = MAGIC + salt + b'$'
    output += _to64((result[0] << 16) | (result[6] << 8) | result[12], 4)
    output += _to64((result[1] << 16) | (result[7] << 8) | result[13], 4)
    output += _to64((result[2] << 16) | (result[8] << 8) | result[14], 4)
    output += _to64((result[3] << 16) | (result[9] << 8) | result[15], 4)
    output += _to64((result[4] << 16) | (result[10] << 8) | result[5], 4)
    output += _to64(result[11], 2)

    return output.decode('utf-8')
