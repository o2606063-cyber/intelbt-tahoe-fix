# Intel 蓝牙在 macOS 26 (Tahoe) 上的修复 —— IntelBTUnified 补丁适配

> 让 **Intel 9260 / 9560 / AX200 / AX210 等 Intel 蓝牙控制器**在 **macOS 26 Tahoe** 上真正可用。
> 本仓库提供**根因分析**、**可复现的二进制补丁脚本**，以及**安装教程**。
> 已在 *Pixelbook Go（i5-8200Y、Intel 9260、macOS 26.7.2 25G332）* 上验证：`State: On`、能搜索、能配对、能稳定连接。

---

## 1. 问题现象

装上社区驱动（[IntelBTUnified](https://github.com/lshbluesky/IntelBTUnified) / IntelBluetoothFirmware + BlueToolFixup）后：

- 固件**能刷进去**（`ioreg` 里 `fw_name` 有值）；
- 但 `bluetoothd` **每 17~30 秒崩一次**，崩溃报告是：

```
exception:  EXC_GUARD
subtype:    GUARD_TYPE_USER
namespc:    18
termination: Application Triggered Fault
```

- `system_profiler SPBluetoothDataType` 永远是：

```
Address: NULL
State: Off
Chipset: BCM_4350C2
```

也就是说：**驱动在跑、固件在跑，但蓝牙栈起不来**。

## 2. 根因

用反汇编定位到 `bluetoothd`（x86_64 切片）里的设备识别函数 `GetProductAndVendorID`（偏移 `0xee16a`）。

它对每一个 `IOUSBHostDevice` 读 6 个属性：

```
USB Vendor Name / USB Product Name / idVendor / idProduct / bDeviceClass / locationID
```

然后做两个判定：

```asm
; (1) 名字判定：只有产品名正好等于 "Bluetooth USB Host Controller" 才算「内置控制器」
call  CFStringCompare(CFString "Bluetooth USB Host Controller", "USB Product Name")
test  rax, rax
je    0xee660                 ; 相等 → 记为内置，取 VID/PID

; (2) 否则走特殊 VID/PID 白名单（Apple USB-C 耳机转接头、TI 设备），再不行就查「第三方 dongle 白名单」
```

**关键问题**：Pixelbook Go 上这颗 9260 的 USB 设备**根本没有 `USB Vendor Name` / `USB Product Name` 这两个属性**（只有 `idVendor` / `idProduct` / `bDeviceClass` / `locationID`）。于是：

1. 名字比较拿不到值 → 不可能是“内置控制器”；
2. VID/PID 也不在白名单（`0x05AC:0x110A`、`0x0451:0x3410`）→ 也不会被当成“认可的第三方 dongle”；
3. 函数返回“没找到”，调用方拿不到 VID/PID → 输出：

```
[bm3_usb] Can't obtain vendorID and productID -- try again
Failed to initialize transport USB
```

4. 传输层失败后走进错误处理器 `0x15950a`，里面直接 `os_fault_with_payload()` **主动自杀**：

```asm
0x15950a: ... 记日志 "Transport layer initialization failed"
0x1595f1: call os_fault_with_payload    ; → EXC_GUARD / namespc 18
```

**社区补丁为什么失效**：IntelBTUnified / BlueToolFixup 的字节模式都是照着 **macOS ≤15.4** 的代码写的，在 26.7.2 上：
- `vendor check` 模式还能命中 1 处；
- 三个 `chipset range check` 模式**全部 0 命中**。

改了一半、没改一半，逻辑反而更不一致 —— 这就是“半生效补丁”的典型故障。

## 3. 修复方案

两个**等长字节替换**，直接改 kext 二进制里的“针/补”常量，**不需要重新编译**：

### 补丁 A —— 强制走“外部 dongle”分支

`bluetoothd` 里有个全局开关（macOS 26 默认“优先内置控制器”）：

```asm
0xee6dd  cmp byte ptr [rip + 0xa2c7ec], 0
0xee6e4  je  0xee759        ; 开关=0 → 走「外部 dongle」分支
         ; 否则 → "Use internal Bluetooth USB Host Controller" → 必然失败
```

把 `je` 改成 `jmp`，强制走能成功的分支：

```
针: 80 3d ec c7 a2 00 00 74
补: 80 3d ec c7 a2 00 00 eb
```

### 补丁 B —— 跳过错误分支，避免开机那次崩溃

```asm
0x159350  test r14d, r14d
0x159353  jne  0x15950a     ; 有错误码 → 跳进“记日志 + 自杀”
```

把 `jne` 换成 6 个 `NOP`，让流程继续走正常路径：

```
针: 45 85 f6 0f 85 b1 01 00 00 31 ff e8
补: 45 85 f6 90 90 90 90 90 90 31 ff e8
```

### 补丁放在 kext 的哪个槽位（很重要）

IntelBTUnified 把每条补丁写成一对“针/补”常量。实测（用**有可观测副作用**的补丁做对照实验）发现：

| 槽位 | 是否会被执行 |
|---|---|
| 15.4 chipset 槽（`0x8352 / 0x835A`） | ✅ 会 —— 补丁 A 放这里 |
| address 槽（`0x8380 / 0x838C / 0x8398`，需 `-btlfxallowanyaddr`） | ✅ 会 —— 补丁 B 放这里 |
| vendor 槽（`0x8326 / 0x832D`） | ✅ 会，但**不能动**：移走 Broadcom 强制补丁后蓝牙立刻回到 `Off` |
| chipset 槽（`0x8334 / 0x833B`）、NVRAM 槽 | ❌ 改了没有任何反应 |

所以**不要**把补丁随便塞进空槽位——先做对照实验确认它真的会被执行。

## 4. 安装教程

### 前置条件

- macOS 26.x（Tahoe），OpenCore 引导
- 已经能加载 Lilu
- 已下载 **IntelBTUnified** 原版 kext（`IntelBTUnified.kext`，来自其 Releases）
- **不要**同时加载旧的三个 kext（IntelBluetoothFirmware、IntelBTPatcher、BlueToolFixup），否则补丁会打两遍

### 懒人版：直接用预编译 kext

到 [Releases](https://github.com/o2606063-cyber/intelbt-tahoe-fix/releases) 下载 `IntelBTUnified-fixed-macOS26.zip`，解压得到 `IntelBTUnified.kext`，
sha256 应为 `6d9e18eb929578c90014f26f745c294db16ca05acd4f64d14cef918e7fc90ab6`，然后直接跳到第 2 步。

### 步骤

1. **生成修复版 kext**

   ```bash
   python3 apply-fix.py --input /path/to/IntelBTUnified.kext --output build/
   ```

   脚本会校验原文件的槽位内容，然后写入两条补丁，输出 `build/IntelBTUnified.kext`。

2. **放到 EFI**

   ```
   EFI/OC/Kexts/IntelBTUnified.kext
   ```

3. **改 `config.plist`**

   在 `Kernel → Add` 里加一条（**必须在 Lilu 之后**）：

   | Key | Value |
   |---|---|
   | BundlePath | `IntelBTUnified.kext` |
   | ExecutablePath | `Contents/MacOS/IntelBTUnified` |
   | PlistPath | `Contents/Info.plist` |
   | Enabled | `true` |
   | Arch | `Any` |
   | MinKernel / MaxKernel | 留空 |

4. **加启动参数**

   `NVRAM → Add → 7C436110-AB2A-4BBB-A880-FE41995C9F82 → boot-args` 里追加：

   ```
   -btlfxallowanyaddr
   ```

   （这条参数是**必须的**：补丁 B 放在 address 槽，靠它开启。）

   建议顺手加 `msgbuf=1048576`，方便看内核日志。

5. **重启**，然后验证。

### 一键推到 Mac（可选）

```bash
python3 apply-fix.py --input IntelBTUnified.kext --install --host 192.168.50.157
```

会用 SSH 把 kext 拷进内部 ESP（默认账号 `oliveryu`，脚本里改）。

## 5. 验证

```bash
# 1) 控制器状态
system_profiler SPBluetoothDataType | head -20
```

期望：

```
Address: 5C:80:B6:...
State: On
Chipset: THIRD_PARTY_DONGLE
Firmware Version: v256 c256
LMP: 5.1
```

```bash
# 2) 没有崩溃
ls -lt /Library/Logs/DiagnosticReports | grep -i bluetooth | head -3

# 3) 真的在扫描（能看到 DeviceFound）
log show --last 5m --predicate 'process == "bluetoothd"' | grep -E "DeviceFound|Found external Bluetooth dongle"
```

`Found external Bluetooth dongle -- vendorID = 0x8087, produceID = 0x0025` 就是补丁 A 生效的标志。

## 6. 已知限制

- 控制器身份是 `THIRD_PARTY_DONGLE`（第三方 dongle）。这是**故意**的：走内置判定必然失败。副作用是部分依赖“内置控制器”的特性（如某些 Continuity 功能）不会启用。
- 与前三个旧 kext **互斥**，不能同时加载。
- 补丁是针对 **macOS 26.7.2 (25G332)** 的 `bluetoothd` 二进制写的。**macOS 升级后必须重新确认偏移和字节**（脚本会校验，不匹配就报错，不会写坏文件）。
- 只验证了 Intel 9260。其他 Intel 型号（AX200/AX210 等）大概率走同一条代码路径，但未实测。

## 7. macOS 升级后怎么重新适配

1. 把新的 `/usr/sbin/bluetoothd` 取下来，`lipo` 出 x86_64 切片；
2. 用 `capstone` 反汇编，按本仓库 `docs/troubleshooting-2026-11-09.md` 里记录的特征找两个位置：
   - `IOServiceMatching("IOUSBHostDevice")` 之后那段属性读取 + `CFStringCompare`；
   - 错误处理器里的 `call os_fault_with_payload`（`ns=18`）向上的 `test/jne`。
3. 改成等长字节替换，写进对应槽位，重启验证。

## 8. 致谢

- [lshbluesky/IntelBTUnified](https://github.com/lshbluesky/IntelBTUnified) —— 本修复基于它的 kext 结构
- [OpenIntelWireless/IntelBluetoothFirmware](https://github.com/OpenIntelWireless/IntelBluetoothFirmware) —— 原始固件驱动
- [acidanthera/Lilu](https://github.com/acidanthera/Lilu) —— 插件框架

## 9. License

GPL-3.0（与原项目一致）。`bluetoothd` 属于 Apple，本仓库**不包含**任何 Apple 二进制文件，只包含补丁的字节常量与偏移。
