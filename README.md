# Apple Watch 心率桌面挂件

这是一个面向 Windows 10/11 的轻量心率显示工具。Apple Watch 负责测量心率，iPhone 上的广播软件把心率转换为标准蓝牙心率带信号，Windows 程序负责接收和显示。

![心率挂件预览](./assets/预览.png)

## 功能

- 透明桌面挂件，显示爱心、实时心率和可选心率区间
- 自动搜索、快速连接、断线重连和无数据自动恢复
- 右键选择指定设备，显示设备名称、MAC 地址和心率服务状态
- 鼠标穿透、全局热键恢复、拖动位置和设置持久化
- 网页大屏、实时曲线、统计、全屏、置顶小窗和 CSV 导出
- 免安装 Windows 版本，不要求目标电脑安装 Python

## 数据链路

```text
Apple Watch → iPhone 广播软件 → BLE 标准心率服务 → Windows 挂件
```

Apple Watch 不会作为本项目的直接连接设备出现。使用 HeartCast 时，Windows 中的设备通常显示为 `HeartCast-iPhon`。设备的蓝牙地址可能在重新广播后变化，遇到这种情况可在挂件右键菜单中重新选择。

## 手机端推荐软件

### 首选：HeartCast

HeartCast 适合 iPhone + Apple Watch。它会在 Apple Watch 上启动一次体能训练，然后由 iPhone 通过标准 BLE 心率带协议广播。

- App Store：[HeartCast: Heart Rate Monitor](https://apps.apple.com/cn/app/heartcast-heart-rate-monitor/id1499771124)
- iPhone 与 Apple Watch 都打开 HeartCast
- 在手表端点 `Start`
- 首次被电脑发现前，让 iPhone 保持解锁，并将 HeartCast 留在前台

### 中文替代

可在 App Store 搜索“心率广播 Heart Rate Monitor”，开发者为“坤 倪”。选择替代软件时，应确认它支持把心率广播成标准 BLE Heart Rate Service（服务 UUID `0x180D`）。

本项目不隶属于上述应用，也不包含这些应用的软件包。

## 免安装版使用方法

1. 下载 [`心率挂件-连接优化版-Windows-x64-20261105.zip`](./releases/心率挂件-连接优化版-Windows-x64-20261105.zip)。
2. 完整解压压缩包，保留 `心率挂件.exe` 与 `_internal` 文件夹的相对位置。
3. 在 iPhone 和 Apple Watch 上打开 HeartCast，手表端点 `Start`。
4. 双击 `心率挂件.exe`。程序发现 HeartCast 广播后会立即连接。
5. 附近有多个心率设备时，右键挂件并选择“选择设备…”，单击目标设备以记住地址。

短暂断线时，程序会先直连刚才验证过的设备。连接存在但连续 20 秒没有有效心率数据时，程序会自动恢复连接。

## 设备选择说明

设备窗口会边扫描边显示结果，不必等待扫描结束。列表中的信息包括名称、MAC 地址、是否广播心率服务和连接状态。

- `HeartCast-iPhon` 通常就是 iPhone 上的 HeartCast 广播
- “是”表示广播中包含标准心率服务
- “是（已验证）”表示程序已经成功订阅该设备的心率数据
- “未广播”表示当前广播包没有携带服务标识，仍可尝试连接
- 当前连接设备会固定在列表顶部

## 网页大屏

双击 `启动.bat`，使用 Chrome 或 Edge 打开网页。点击“连接设备”后选择 HeartCast。

| 快捷键 | 功能 |
|---|---|
| 空格 | 连接或断开 |
| `M` | 打开或关闭置顶小窗 |
| `F` | 切换全屏 |
| `D` | 演示模式 |
| `E` | 导出 CSV |

浏览器对本地文件的蓝牙权限有限时，可双击 `服务器模式.bat`，然后访问 `http://127.0.0.1:8765/heart-rate.html`。

## 常见问题

### 找不到 HeartCast

确认手表端已经点 `Start`，iPhone 已解锁，HeartCast 在前台，并让手机距离电脑 1～2 米。不要在 Windows 蓝牙设置里提前配对 HeartCast。标准心率带协议由程序直接连接。

### 已连接但数字不更新

确认 Apple Watch 仍在测量心率，HeartCast 训练没有结束。程序连续 20 秒收不到心率时会自动重连。详细原因记录在同目录的 `挂件.log`。

### 游戏里看不到挂件

将游戏显示模式改为“无边框窗口”或“窗口化全屏”。独占全屏通常会遮住桌面挂件。

### 鼠标无法点击挂件

如果开启了鼠标穿透，按 `Ctrl + Alt + H` 恢复点击。

## 从源码运行

需要 Windows、Python 3.11 或更高版本，以及支持 BLE 的适配器。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python heart_overlay.pyw
```

诊断扫描：

```powershell
python heart_overlay.pyw --scan
```

演示模式：

```powershell
python heart_overlay.pyw --demo
```

## 自行构建 Windows 免安装版

```powershell
.\build.ps1
```

构建结果位于 `dist\心率挂件\`。构建脚本会安装固定版本的依赖并调用 PyInstaller。

## 项目文件

| 文件 | 作用 |
|---|---|
| `heart_overlay.pyw` | 桌面挂件主程序 |
| `heart-rate.html` | 网页大屏 |
| `心率挂件.bat` | 从源码启动桌面挂件 |
| `启动.bat` | 打开网页大屏 |
| `服务器模式.bat` | 通过本地 HTTP 服务打开网页 |
| `诊断扫描.bat` | 扫描 BLE 设备 |
| `挂件设置.example.json` | 设置示例 |
| `tests/` | 连接、重连和设备选择回归测试 |

## 隐私

心率数据在 Apple Watch、iPhone 和本机之间通过蓝牙传输。桌面挂件不会将心率上传到服务器。网页大屏的数据保存在当前浏览器页面内，只有主动导出 CSV 时才会生成本地文件。
