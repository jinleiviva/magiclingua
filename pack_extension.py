#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 extension/ 打包成 CRX3。

关键点：签名必须用同一把 extension.pem —— 扩展 ID 由公钥推导，
换钥匙 ID 就变了，Chrome 会当成另一个扩展，Native Messaging 的
allowed_origins 也随之失效。

用法：  python3 pack_extension.py
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(BASE, "extension")
PEM = os.path.join(BASE, "extension.pem")
OUT = os.path.join(BASE, "extension.crx")

SKIP_NAMES = {".DS_Store", "extension.crx", "extension.pem"}
SKIP_SUFFIX = (".crx", ".pem")


# --------------------------------------------------------------- protobuf --

def pb_varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_bytes(field, data):
    """只用到 length-delimited 字段，足够拼 CRX 头。"""
    return pb_varint((field << 3) | 2) + pb_varint(len(data)) + data


# --------------------------------------------------------------- 公钥 / ID --

def public_key_der(pem_path):
    out = subprocess.run(
        ["openssl", "rsa", "-in", pem_path, "-pubout", "-outform", "DER"],
        capture_output=True, check=True,
    )
    return out.stdout


def extension_id(pub_der):
    """
    Chrome 扩展 ID：公钥 SHA256 前 16 字节 -> 32 位十六进制，
    再把每个十六进制字符 0-f 映射成 a-p（Chrome 的 ConvertHexadecimalToIDAlphabet）。
    """
    digest = hashlib.sha256(pub_der).digest()[:16]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest.hex())


def rsa_sign(pem_path, data, workdir):
    msg = os.path.join(workdir, "msg.bin")
    sig = os.path.join(workdir, "sig.bin")
    with open(msg, "wb") as f:
        f.write(data)
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", pem_path, "-out", sig, msg],
        check=True, capture_output=True,
    )
    with open(sig, "rb") as f:
        return f.read()


# ------------------------------------------------------------------- 打包 --

def build_zip(zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(EXT_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(files):
                if name in SKIP_NAMES or name.startswith(".") or name.endswith(SKIP_SUFFIX):
                    continue
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, EXT_DIR))
    return os.path.getsize(zip_path)


def build_crx(zip_path, pub_der, sign_fn):
    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    crx_id = hashlib.sha256(pub_der).digest()[:16]
    signed_header_data = pb_bytes(1, crx_id)

    # header_length 参与签名，而签名又决定 header 长度 —— 迭代到稳定
    header_length = 0
    for _ in range(8):
        to_sign = b"Cr24" + (3).to_bytes(4, "little") + \
            header_length.to_bytes(4, "little") + signed_header_data
        signature = sign_fn(to_sign)

        proof = pb_bytes(1, pub_der) + pb_bytes(2, signature)
        header = pb_bytes(2, proof) + pb_bytes(10000, signed_header_data)

        if len(header) == header_length:
            break
        header_length = len(header)

    return (b"Cr24"
            + (3).to_bytes(4, "little")
            + header_length.to_bytes(4, "little")
            + header
            + zip_bytes)


def main():
    if not os.path.exists(PEM):
        print("缺少 extension.pem，无法签名")
        return 1

    pub_der = public_key_der(PEM)
    ext_id = extension_id(pub_der)

    workdir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(workdir, "ext.zip")
        size = build_zip(zip_path)

        crx = build_crx(zip_path, pub_der, lambda d: rsa_sign(PEM, d, workdir))
        with open(OUT, "wb") as f:
            f.write(crx)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("打包完成")
    print("  扩展 ID : %s" % ext_id)
    print("  文件    : %s" % OUT)
    print("  内容    : %.1f KB" % (size / 1024))
    print("  签名    : %.1f KB" % (os.path.getsize(OUT) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
