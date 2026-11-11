# Windows 原生 Roc Sender

桌面版支持设备与端点配置、系统托盘及可选登录启动，使用与打包方式见 [DESKTOP.md](DESKTOP.md)。

这个项目在纯 Windows 环境中从 WASAPI 录音输入或 Loopback 捕获音频，并通过原生
`roc.dll` 发送 `RTP+RS8M`、`RS8M repair` 和 `RTCP`。协议和 FEC 都由 Roc
Toolkit 实现，没有降级到普通 RTP。

## 系统要求

- 64 位 Windows 10 1803 或更新版本。Windows 实现使用高精度 waitable timer，
  系统不支持时会明确失败。
- PowerShell 7、Git 和 Conda 已加入 `PATH`。
- 要捕获 VB-CABLE 的声音，需已安装 VB-Audio Virtual Cable，并将播放路由到
  `CABLE Input`；默认从它对应的 `CABLE Output` 录音端采集。

## 首次设置和构建

在 PowerShell 7 中运行：

```powershell
./setup.ps1
./build.ps1
```

`setup.ps1` 创建隔离的 `.conda` 环境，检出固定提交的 Roc Toolkit、libuv 和
OpenFEC，校验并解压固定版本 LLVM-MinGW 与 Ragel，然后应用
`patches/roc-windows.patch`。`build.ps1` 生成：

```text
build/Release/roc.dll
build/Release/libc++.dll
build/Release/libunwind.dll
```

## 配置和发送

先查看当前机器的 WASAPI 录音输入和 Loopback 设备（列表标明采集类型）：

```powershell
conda run --prefix ./.conda python sender.py --list-devices
```

复制并编辑 `config.example.toml`。示例接收地址 `192.0.2.1` 仅用于文档；运行前应替换为
接收端地址。示例使用 `capture_mode = "input"` 和
`device = "CABLE Output"`，从对应录音端采集。只有 WASAPI 设备参与选择，
不会选到 MME 等其他接口的同名设备，也不会自动切换设备或采集模式。

设备名按不区分大小写的子串匹配；匹配
不到或匹配到多个设备都会报错，也可以设置本次枚举得到的 `device_index`。
接收端三项必须全部提供，并分别使用 `rtp+rs8m://`、`rs8m://`、`rtcp://`。

```powershell
conda run --prefix ./.conda python sender.py --config config.example.toml
```

若要捕获其他播放输出，显式选择 `loopback`，同时指定列表中的输出名称或索引：

```powershell
conda run --prefix ./.conda python sender.py --config config.example.toml --capture-mode loopback --device "Realtek"
```

`input` 与 `loopback` 均使用 WASAPI 回调采集，默认每块 480 帧（48 kHz 时 10 ms）。
回调只把 PCM 交给有界队列，主线程立即编码发送；不等待队列填满。队列容量为
100 ms 向上取整到完整块（默认 10 块），这是积压上限，不是新增固定延迟。
驱动报告异常、音频块不完整或发送赶不上导致队列满，均明确报错停止，不静默丢帧。
Roc 编码、网络参数和采样率没有因改用回调而变化。

运行中每 10 秒打印一条 `Health`：该窗口采集/发送帧数、PCM 峰值、当前队列和距
最后一次回调的时间。可通过 `--stats-interval 30` 调整间隔。`silent PCM` 表示收到
并发送了全零音频，`no captured PCM` 表示窗口内没有收到新音频；后者不等于用户没有
播放。这些指标也不能证明接收端正在播放，只描述本端采集和提交给 Roc 的情况。

2026-09-21 的 `WASAPI capture stopped unexpectedly` 尚未在独立短测中复现，不能
视作已修复。异常现在附带最后回调时间、状态和队列信息；回调边界异常会保留原始
原因传回主线程，避免活动状态检查的竞争把具体错误遮蔽成通用停止错误。
当前 PyAudioWPatch 不提供原生 WASAPI 停止 HRESULT，仍需该信息才能确定底层原因。
运行期间改变设备采样率可能使既有流失效；调整后应停止旧 sender，再启动以重新
读取设备格式。程序不会静默重连、换设备或更换采样率。

有限时长检查：

```powershell
conda run --prefix ./.conda python sender.py --config config.example.toml --duration 5
```

停止时先停止采集，再发送队列中已接受的所有音频，报告采集/发送帧数、音频时长、
实际经过时间、峰值及队列最高占用。峰值为零会打印明确警告，通常表示
音频没有路由到选中的采集设备。

## 验证

```powershell
conda run --prefix ./.conda python -m unittest -v test_sender.py
conda run --prefix ./.conda python test_native_loopback.py
```

第二项在本机 UDP 回环上启动同一个原生 DLL 的 receiver 和 sender，发送合成
音频并验证经过 RTP/RS8M/RTCP 管线后能解码出非静音 PCM。它覆盖 DLL 加载、
网络绑定、Roc 编解码、OpenFEC 集成和关闭流程。

此前的实机结果：原生 loopback
解码峰值为 `0.312941`；`CABLE Input (VB-Audio Virtual Cable) [Loopback]`
以 48 kHz 双声道实际采集 2.98 秒并向接收端的 10001/10002/10003
端口发送，采集峰值为 `0.001007`。

改为录音端后，7 个行为测试通过。独立 12 秒实测选中 WASAPI
`CABLE Output (VB-Audio Virtual Cable)`（本次索引 39，48 kHz 双声道），
使用 480 帧阻塞读取：前段 6.009053 秒采集 278400 帧（46330.097 帧/秒），
后段 6.005957 秒采集 282720 帧（47073.261 帧/秒），峰值 `0.232552`。
这个采集速率仍低于标称 48000 帧/秒，因此仅凭此结果不能认定接收端卡顿已消失。

改用回调后，11 个行为测试通过。CABLE Output → 本机 UDP 的 30 秒实测：
采集和发送均为 1439520 帧，29.99 秒音频 / 30.000 秒实际时间，队列最高
2/10 块，没有报告异常。这次源音频为静音（程序已警告），因此该结果验证
采集节拍和帧数守恒，不代表远端播放音乐的听感验证已经完成。
随后非静音检查：CABLE Output 5.000 秒采集并发送 240000 帧，峰值 0.249630，
队列最高 1/10；CABLE Input Loopback 5.000 秒采集并发送 239520 帧，
峰值 0.249111，队列最高 2/10。

## 固定的上游版本

| 组件 | 版本或提交 |
| --- | --- |
| Roc Toolkit | `60db91cbba666972845d190d981bc0d093298699` |
| libuv | `5152db2cbfeb5582e9c27c5ea1dba2cd9e10759b` (v1.51.0) |
| OpenFEC | `e549f3b8ba54a7d6e2aaefdd1cdd4c73ef32000c` |
| LLVM-MinGW | `20260908`, UCRT x86_64 |
| Ragel | `6.10` |

`vendor/` 不提交到项目；所有 Windows 平台实现和必要的 LLP64 可移植性修复都
保存在 `patches/roc-windows.patch`，因此可从干净上游检出重建。

## 许可证

Roc Sender 自有代码采用 MIT 许可证，见根目录的 `LICENSE`。`patches/roc-windows.patch`
修改了采用 MPL-2.0 的 Roc Toolkit 源文件；该补丁及对应修改后的 Roc 源文件继续遵循
MPL-2.0，许可证文本见 `licenses/Roc-MPL-2.0.txt`。可通过 `setup.ps1` 获取固定提交的
Roc Toolkit 源码并应用该补丁。

第三方组件仍采用各自许可证；仓库中已收录的第三方许可证副本在 `licenses/` 中。
根目录的 MIT 许可证不会替代或覆盖这些第三方许可证。
