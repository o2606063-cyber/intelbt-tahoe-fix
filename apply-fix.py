#!/usr/bin/env python3
"""给 IntelBTUnified 打上 macOS 26 (Tahoe) 适配补丁 —— 免重新编译。

用法
----
    python3 apply-fix.py --input IntelBTUnified.kext --output build/
    python3 apply-fix.py --input IntelBTUnified.kext --install --host 192.168.50.157

它做什么
--------
在 kext 二进制的“针/补”常量表里写入两条等长替换：

  补丁 A（15.4 chipset 槽）—— 强制 bluetoothd 走“外部 dongle”分支
      针 80 3d ec c7 a2 00 00 74  →  补 80 3d ec c7 a2 00 00 eb   (je → jmp)

  补丁 B（address 槽，需 boot-arg -btlfxallowanyaddr）—— 跳过错误分支，防止开机崩溃
      针 45 85 f6 0f 85 b1 01 00 00 31 ff e8
      补 45 85 f6 90 90 90 90 90 90 31 ff e8

为什么是这两个位置、以及为什么只能放这两个槽，见 README 与 docs/。
"""

import argparse
import hashlib
import os
import shutil
import sys

# kext 二进制里各槽位的偏移 + 原始内容（用于校验，防止改错版本）
EXPECTED = {
    "vendor_needle":  (0x8326, bytes([0x81, 0xFA, 0x5C, 0x0A, 0x00, 0x00, 0x74])),
    "chipset_needle": (0x8334, bytes([0x81, 0xF9, 0xCF, 0x07, 0x00, 0x00, 0x72])),
    "chipset15_needle": (0x8352, bytes([0x81, 0xF9, 0x6F, 0x17, 0x00, 0x00, 0x77, 0x30])),
    "address_needle": (0x8380, bytes([0x48, 0x89, 0xF3, 0xE8, 0x00, 0x00, 0x00, 0x00, 0x85, 0xC0, 0x74, 0x1D])),
}

PATCH_A_NEEDLE = bytes([0x80, 0x3D, 0xEC, 0xC7, 0xA2, 0x00, 0x00, 0x74])
PATCH_A_VALUE  = bytes([0x80, 0x3D, 0xEC, 0xC7, 0xA2, 0x00, 0x00, 0xEB])

PATCH_B_NEEDLE = bytes([0x45, 0x85, 0xF6, 0x0F, 0x85, 0xB1, 0x01, 0x00, 0x00, 0x31, 0xFF, 0xE8])
PATCH_B_VALUE  = bytes([0x45, 0x85, 0xF6, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90, 0x31, 0xFF, 0xE8])

BIN_REL = os.path.join("Contents", "MacOS", "IntelBTUnified")


def patch_kext(src, outdir):
    if not os.path.isdir(src):
        sys.exit("找不到 kext 目录: %s" % src)
    binary = os.path.join(src, BIN_REL)
    if not os.path.isfile(binary):
        sys.exit("kext 里没有 %s，确认你给的是 IntelBTUnified.kext" % BIN_REL)

    dst = os.path.join(outdir, "IntelBTUnified.kext")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(outdir, exist_ok=True)
    shutil.copytree(src, dst)

    target = os.path.join(dst, BIN_REL)
    with open(target, "rb") as fh:
        data = bytearray(fh.read())

    for name, (off, expect) in EXPECTED.items():
        got = bytes(data[off:off + len(expect)])
        if got != expect:
            sys.exit(
                "槽位 %s 校验失败\n  期望: %s\n  实际: %s\n"
                "说明这个 kext 不是预期的版本（或已被改过），请用原版重新下载。"
                % (name, expect.hex(), got.hex())
            )

    data[0x8352:0x835A] = PATCH_A_NEEDLE
    data[0x835A:0x8362] = PATCH_A_VALUE

    data[0x8380:0x838C] = PATCH_B_NEEDLE
    data[0x838C:0x8398] = bytes([0xFF] * 12)   # mask 全 FF（地址槽原本是掩码匹配）
    data[0x8398:0x83A4] = PATCH_B_VALUE

    with open(target, "wb") as fh:
        fh.write(bytes(data))

    digest = hashlib.sha256(bytes(data)).hexdigest()
    print("[ok] 输出: %s" % dst)
    print("[ok] 二进制 sha256: %s" % digest)
    print("[!] 别忘了 boot-args 加 -btlfxallowanyaddr")
    return dst, digest


def install(kext_dir, host, user, password):
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, 22, username=user, password=password, timeout=20,
                look_for_keys=False, allow_agent=False)
    sftp = cli.open_sftp()

    def sudo(cmd):
        _, out, err = cli.exec_command(
            "echo '%s' | sudo -S -p '' bash -c \"%s\"" % (password, cmd.replace('"', '\\"')),
            timeout=300)
        return out.read().decode(errors="replace").strip(), err.read().decode(errors="replace").strip()

    print(sudo("diskutil mount disk0s1")[0])

    def upload(local, remote):
        try:
            sftp.mkdir(remote)
        except IOError:
            pass
        for name in os.listdir(local):
            lp, rp = os.path.join(local, name), remote + "/" + name
            if os.path.isdir(lp):
                upload(lp, rp)
            else:
                sftp.put(lp, rp)

    cli.exec_command("rm -rf /tmp/kfix && mkdir -p /tmp/kfix")[1].read()
    upload(kext_dir, "/tmp/kfix/IntelBTUnified.kext")
    out, err = sudo("rm -rf /Volumes/EFI/EFI/OC/Kexts/IntelBTUnified.kext && "
                    "cp -R /tmp/kfix/IntelBTUnified.kext /Volumes/EFI/EFI/OC/Kexts/ && echo installed")
    print(out or err)
    print(sudo("diskutil unmount /Volumes/EFI")[0])
    cli.close()
    print("[ok] 已拷入 ESP。确认 config.plist 的 Kernel→Add 和 boot-args 后重启。")


def main():
    ap = argparse.ArgumentParser(description="IntelBTUnified macOS 26 适配补丁")
    ap.add_argument("--input", default="IntelBTUnified.kext", help="原版 IntelBTUnified.kext 路径")
    ap.add_argument("--output", default="build", help="输出目录")
    ap.add_argument("--install", action="store_true", help="打完后通过 SSH 装进 Mac 的 ESP")
    ap.add_argument("--host", default="192.168.50.157")
    ap.add_argument("--user", default="oliveryu")
    ap.add_argument("--password", default="jason12345")
    args = ap.parse_args()

    kext, _ = patch_kext(args.input, args.output)
    if args.install:
        install(kext, args.host, args.user, args.password)


if __name__ == "__main__":
    main()
