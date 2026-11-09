# 排障全记录：macOS 26.7.2 上让 Intel 9260 蓝牙可用

硬件：Pixelbook Go（board `atlas`，i5-8200Y），蓝牙 = USB `8087:0025`（Intel 9260）
系统：macOS 26.7.2（25G332），OpenCore 1.0.8
日期：2026-11-09

---

## 一、初始现象

加载 IntelBTUnified（或 IntelBluetoothFirmware + BlueToolFixup）后：

- 固件刷入成功：`ioreg -r -n 'Bluetooth USB Host Controller'` → `fw_name` 有值；
- `bluetoothd` 每 17~30 秒崩一次：
  `EXC_GUARD / GUARD_TYPE_USER / namespc 18 / Application Triggered Fault`
- 控制器：`Address: NULL`、`State: Off`、`Chipset: BCM_4350C2`

崩溃报告里的栈（`usedImages[0]` 是 bluetoothd）：

```
os_fault_with_payload
bluetoothd  +0x1595c3  ← 调用点
bluetoothd  +0xe8360
```

## 二、逐项排除（都做了实测）

| 假设 | 做法 | 结果 |
|---|---|---|
| 补丁没打上（模式失配） | 用 `bytes.count()` 逐条核对 kext 里的针 | vendor 针命中 1 处；3 条 chipset 针 **0 命中** |
| 是补丁把进程弄崩的 | 加 `-ibtoff`（插件全关）重启 | ✅ bluetoothd 稳定，但蓝牙不可用 |
| 只关 vendor 补丁 | 改针让 vendor 永不匹配 | ❌ 仍崩 |
| 所有补丁都关 | vendor + skip-update 针一起清零 | ❌ 仍崩 |
| HCI 钩子（`deviceRequest`）破坏了描述符读取 | 破坏符号名关掉钩子 | ❌ 日志一模一样 |
| 固件没刷进去 | `-btlfxboardid` / `-btlfxallowanyaddr` / `bluetoothHostControllerSwitchBehavior=never` | ❌ 都无效 |
| 冷启动会重新刷固件、触发重新枚举 | `shutdown -h` 断电再开机 | ❌ `fw_name` 仍是 `(already running)`（9260 固件常驻） |
| 固件驱动占着设备 | `kextunload` + `killall bluetoothd` | ❌ kextunload 被拒（in use），重初始化后现象相同 |

结论：**不是补丁“打歪”，而是它在 patches 之外还有一层失败**。

## 三、定位真正的失败点

把 `/usr/sbin/bluetoothd` 取下来，`lipo` 出 x86_64 切片（`__TEXT` vmaddr `0x100000000`，切片内 `VA = 0x100000000 + offset`），用 capstone 反汇编：

### 1) 失败时的日志

```
[bm3_usb] Found USB Device :
[bm3_usb] Use internal Bluetooth USB Host Controller
[bm3_usb] Can't obtain vendorID and productID -- try again
Failed to initialize transport USB
[Stack.HCI] reset command failed (STATUS 1205)
```

### 2) `GetProductAndVendorID`（0xee16a）在做什么

```asm
rdi = "IOUSBHostDevice"
IOServiceMatching / IOServiceGetMatchingServices
loop 每个 IOUSBHostDevice:
    IORegistryEntryCreateCFProperties
    CFStringCreateWithCString("USB Vendor Name" / "USB Product Name" / "idProduct" / "bDeviceClass" / "locationID")
    CFDictionaryGetValue + CFNumberGetValue   ; 6 个属性缺一个就跳过
    ; 设备类过滤：只接受 bDeviceClass ∈ {0xE0, 0xEF, 0xFF}
    ; 名字比较：
    CFStringCompare(CFString("Bluetooth USB Host Controller"), 产品名)
    test rax,rax ; je 0xee660        → 相等才算「内置控制器」
    ; 否则：Apple USB-C 转接头 05AC:110A / TI 0451:3410
    ; 再否则：call 0xed0c0  ← 第三方 dongle 白名单
```

### 3) 这台设备的属性

```
idVendor = 0x8087   idProduct = 0x0025   bDeviceClass = 0xE0   locationID = ...
USB Vendor Name     ← 不存在
USB Product Name    ← 不存在
```

**根因**：名字属性不存在 → 不可能是“内置”；VID/PID 不在白名单 → 也不是“认可的 dongle” → 函数返回失败 → 传输层初始化失败 → 走错误处理器自杀。

### 4) 错误处理器

```asm
0x159350  test r14d, r14d          ; r14d = 传输层错误码
0x159353  jne  0x15950a            ; 有错误 → 跳进错误处理器
...
0x15950a  ... "Transport layer initialization failed"
0x1595f1  call os_fault_with_payload(ns=0x12, reason=2)   ← 主动自杀
```

## 四、修复

### 补丁 A：强制走“外部 dongle”分支

```asm
0xee6dd  cmp byte ptr [rip+0xa2c7ec], 0
0xee6e4  je  0xee759     ; 开关=0 → 外部 dongle 分支（能成功）
```

改成无条件跳转：

```
针: 80 3d ec c7 a2 00 00 74
补: 80 3d ec c7 a2 00 00 eb
```

### 补丁 B：跳过错误分支（消除开机那一次崩溃）

```
针: 45 85 f6 0f 85 b1 01 00 00 31 ff e8
补: 45 85 f6 90 90 90 90 90 90 31 ff e8
```

### 放在哪个槽位：一次关键的对照实验

IntelBTUnified 的补丁常量按槽位排列（kext 二进制偏移）：

```
0x8326  vendor 针            (7B)
0x832d  vendor 补            (7B)
0x8334  chipset 针           (7B)
0x833b  chipset 补           (7B)
0x8342  chipset 13.3 针      (8B)
0x834a  chipset 13.3 补
0x8352  chipset 15.4 针      (8B)
0x835a  chipset 15.4 补
0x8362  NVRAM 针 + mask      (10B + 10B)
0x8376  NVRAM 补
0x8380  address 针 + mask    (12B + 12B)
0x8398  address 补
```

一开始把补丁塞进 chipset / NVRAM 槽 → **毫无反应**。把同一条补丁换进 15.4 槽 → 日志立刻从 `Use internal` 变成：

```
Use external Bluetooth USB Host Controller
Found external Bluetooth dongle -- vendorID = 0x8087, produceID = 0x0025
```

**教训**：这个 kext 里只有 **15.4 槽**和 **address 槽**（需 `-btlfxallowanyaddr`）会真正执行；chipset 槽与 NVRAM 槽的补丁改了没有任何作用。而 **vendor 槽不能动**——把 Broadcom 强制那条补丁换掉后，蓝牙立刻回到 `Off`。

## 五、最终配置

| 项 | 值 |
|---|---|
| 补丁 A | 15.4 槽（`0x8352 / 0x835A`） |
| 补丁 B | address 槽（`0x8380 / 0x838C / 0x8398`，mask 全 `FF`） |
| boot-args | `-btlfxallowanyaddr`（必须） |

## 六、验证结果

```
Address: 5C:80:B6:D3:7A:D8
State: On
Chipset: THIRD_PARTY_DONGLE
Firmware Version: v256 c256
LMP: 5.1
Supported services: 0x392039 < HFP AVRCP A2DP HID Braille LEA AACP GATT SerialPort >
```

- 开机后崩溃报告 **0 份**；
- `killall -9 bluetoothd` 强制重启后仍 `On`、0 崩溃（说明每次初始化都成立，不是碰巧）；
- 日志出现 `Inquiry state changed: On`、连续 `decryptProximityPairingPayload`（射频确实在收广播）；
- 实测能发现设备、配对并保持连接（连上了一台 `Minor Type: Mobile Phone` 的设备，`RSSI −64`）。

## 七、踩过的坑（给后来人）

1. **“补丁半生效”比“完全没生效”更危险**：vendor 命中、chipset 全失配，逻辑走到自相矛盾的分支，直接断言。
2. **NOP 断言调用没用**，要 NOP 掉「跳进错误处理器的那条条件跳转」。
3. **固件会常驻**：`shutdown -h` 断电后 9260 的 `fw_name` 仍是 `(already running)`，别假设断电会复位。
4. **改 kext 常量前先确认槽位会被执行**——用有可观测副作用的补丁做对照，别用“看不出变化”的补丁去测。
5. 这台机器的 Wi-Fi 省电会让 SSH 掉线，长任务前先 `sudo pmset -a sleep 0 disablesleep 1`；机器睡着可以用 WOL magic packet 唤醒。
