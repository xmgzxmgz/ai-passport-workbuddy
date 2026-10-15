# AI Passport × WorkBuddy

> 把 [FoloToy AI Passport](https://github.com/FoloToy/ai-passport)（ESP32-C3 桌面挂件）改造成 **WorkBuddy 的随身终端**：
> 挂在显示器边上就能看对话状态、翻最近的消息，按一下直接对着它说话 —— 本地转写成文字、在屏幕上确认，然后投进 PC 上正开着的那个 WorkBuddy 对话。

```
   ┌─────────────────────────────┐
   │  ●  IDLE                    │   ← 状态灯：IDLE / BUSY / OFFLINE
   │                             │
   │  Recent conversations       │   ← 最近 5 条，一屏不翻页
   │  ┌───────────────────────┐  │
   │  │ ● 驾驶舱前端调优        │  │
   │  │ ● 访客系统登录修复      │  │
   │  └───────────────────────┘  │
   │                             │
   │      [ ▲ ] [ OK ] [ ▼ ]     │   ← 三键：翻页 / 确认 / 录音
   └─────────────────────────────┘
```

---

## 它能做什么

| 功能 | 说明 |
|---|---|
| **状态灯** | 顶部圆点实时反映 WorkBuddy 状态：IDLE（绿）/ BUSY（黄）/ OFFLINE（灰） |
| **对话列表** | 一屏 5 条最近会话，附运行状态色标，按 OK 进详情 |
| **消息流** | 气泡式一问一答：你的话靠右、WorkBuddy 靠左，自动换行、可滚动 |
| **语音提问** | 按 OK 开录 → 再按 OK 结束 → BLE 送 PC → 本地 Whisper 转写 |
| **转写回显** | 识别结果先回显到屏幕的 `HEARD` 卡片，**你确认后才真正发送**（按 ▲ 丢弃） |
| **额度查看** | 显示当前用量 / 额度 |
| **全中文 UI** | 内置思源黑体子集（GB2312 全集 + ASCII），固件内自绘 |

**两个刻意的设计选择：**

- 🚫 **不做语音播报。** 没有喇叭，也不用 ES8311 的播放通路 —— 这是个静默终端，只输入不输出。
- 🚫 **对话列表只显示 5 条。** 屏幕一屏 5 行，不做翻页状态机，省 RAM 也省心智。

---

## 架构

```
        ┌──────────────────────────────┐
        │   FoloToy AI Passport        │
        │   ESP32-C3 · LVGL 9 · NimBLE │
        │   240×320 ST7789P3           │
        └───────┬──────────────┬───────┘
                │  FF01 Write  │  FF02 Notify
                │  (下行)      │  (上行)
                ▼              ▲
        ════════╪══════════════╪════════ BLE (FF00 服务, MTU 247)
                │              │
        ┌───────┴──────────────┴─────────────────────────────┐
        │   wb-bridge —— PC 侧中转服务 (Python)               │
        │                                                     │
        │   下行  ~/.workbuddy 数据 ──归约──► 协议帧           │
        │         · sessions/*.json     会话心跳              │
        │         · workbuddy.db        sessions / usage      │
        │         · projects/**/*.jsonl 对话消息流            │
        │                                                     │
        │   上行  按键 / 选择 / ADPCM 语音帧                   │
        │         └─► IMA ADPCM 解码 ─► WAV                   │
        │                                                     │
        │   ASR   faster-whisper（本地，支持 CUDA）            │
        │                                                     │
        │   发送  WorkBuddy 本地网关 REST API                  │
        └─────────────────────────────────────────────────────┘
```

数据流是**单向只读**的：中转服务只读 WorkBuddy 的数据文件，不写、不改。唯一会落笔的地方是「发送」这一步，走的是 WorkBuddy 自己的本地网关。

---

## 硬件

| 项 | 规格 |
|---|---|
| 主控 | ESP32-C3（RISC-V 单核 160MHz，**无 PSRAM**，可用堆 ~170KB） |
| 屏幕 | 240×320 ST7789P3（SPI） |
| 音频 | ES8311 codec（本项目只用 ADC 录音通路） |
| 电量计 | CW2017 |
| 按键 | 三键电阻分压（ADC1_CH0），短按 180ms / 长按 500ms |
| Flash | 8MB |

固件侧对 RAM 极度敏感 —— 这是后面很多设计取舍的根源。

---

## 快速开始

### 1. 固件

本仓库 `firmware/` 存的是**相对上游的改动**，不是完整工程。先取上游再覆盖：

```bash
# 1) 取上游工程
git clone https://github.com/FoloToy/ai-passport.git
cd ai-passport

# 2) 用本仓库的 firmware/ 覆盖上去
cp -r /path/to/ai-passport-workbuddy/firmware/. ./

# 3) 编译
idf.py set-target esp32c3
idf.py build

# 4) 烧录（换成你的串口）
idf.py -p COM18 flash monitor
```

> **中文用户注意**：仓库里的 `firmware/main/fonts/lv_font_wb_{14,16}.c` 是**已生成好的字库**（共 10MB），直接用即可。
> 想改字号或字符集，重新生成：
> ```bash
> npm i lv_font_conv                 # 首次
> cd firmware/fonts && node gen_font.js
> ```

### 2. PC 中转服务

```bash
cd bridge
pip install -r requirements.txt
python wb_bridge.py
```

启动后会打印设备连接状态：

```
  ASR    : faster-whisper/small cached=True
  HTTP   : http://127.0.0.1:8765/
[BLE] connected 4C:11:AE:32:92:1E mtu=247
[18:07:53] BLE 4C:11:AE:32:92:1E mtu=247 pushed=8 recv=0
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WB_WHISPER_MODEL` | `small` | faster-whisper 模型名：`tiny`/`base`/`small`/`medium`/`large-v3` |
| `WB_WHISPER_LANG` | `zh` | 转写语言 |
| `WB_CLI_DIR` | 自动探测 | WorkBuddy CLI 目录（仅兜底路径用） |
| `WB_NODE_EXE` | 自动探测 | node 可执行文件（仅兜底路径用） |
| `WB_ENGINE_ROOT` | `../firmware` | 固件工程根，字库脚本用 |

模型首次运行会从 `hf-mirror.com` 下载并缓存。**想要精度就上 `medium` 或 `large-v3`**，`small` 只适合尝鲜。

### 3. 用起来

| 页面 | 操作 |
|---|---|
| HOME | 顶部状态灯 + 最近对话；按 OK 进列表 |
| LIST | ▲/▼ 选择对话，OK 进入 |
| DETAIL | ▲/▼ 滚动消息气泡；按 OK 进录音页 |
| RECORD | OK 开始录音 → OK 结束 → 等 PC 转写 → `HEARD` 卡片回显 → **OK 确认发送** / ▲ 丢弃 |

---

## BLE 协议

两条通道，都在 `FF00` 服务下：

| 特征 | UUID | 方向 |
|---|---|---|
| FF01 | `0000ff01-0000-1000-8000-00805f9b34fb` | Write，PC → 工牌 |
| FF02 | `0000ff02-0000-1000-8000-00805f9b34fb` | Notify，工牌 → PC |

**帧格式**（两边一致）：

```
[0] magic   [1] cmd   [2] seq   [3] total   [4..] payload
```

> **设计取舍**：一条记录一帧，**不做跨包重组**。MTU 247 下单包可带 ~240 字节有效载荷，足够装下一条对话标题（≤32B）或截断消息（≤120B）。省掉一整层状态机，也不占额外 RAM —— 对没有 PSRAM 的 C3 来说这笔账很划算。

### 下行帧（PC → 工牌，写 FF01）

| cmd | 名称 | payload |
|---|---|---|
| `0x01` | `WB_D_STATUS` | `state, flags, upt16, nameLen, name[]` |
| `0x02` | `WB_D_CONV` | seq=索引 total=总数；`status, titleLen, title[]` |
| `0x03` | `WB_D_MSG` | seq=索引 total=总数；`role, textLen, text[]` |
| `0x04` | `WB_D_USAGE` | ASCII 文本，如 `"12.4 / 30.0 credits"` |
| `0x05` | `WB_D_INFO` | ASCII 文本（模型名 / 提示） |
| `0x06` | `WB_D_VOICE` | `0=READY 1=DENY` |
| `0x07` | `WB_D_SENT` | `0=OK 1=FAIL`, 文本 |
| `0x08` | `WB_D_CLEAR` | `0=全部 1=对话列表 2=消息` |
| `0x09` | `WB_D_ASR` | `0=OK 1=FAIL`, 转写文本 —— **只回显，不发送** |

### 上行帧（工牌 → PC，Notify FF02）

| cmd | 名称 | payload |
|---|---|---|
| `0x01` | `WB_U_KEY` | `btn, ev` |
| `0x02` | `WB_U_SELECT` | `convIndex` |
| `0x03` | `WB_U_REQ` | `WB_REQ_*`（ALL / LIST / MSG / USAGE） |
| `0x04` | `WB_U_VOICE_START` | `rate16, codec8, rsv8` |
| `0x05` | `WB_U_VOICE_DATA` | PCM：`seq16, pcm[]`<br>ADPCM：`seq16, seed16, nibbles[]` |
| `0x06` | `WB_U_VOICE_END` | `chunks16, samples32` |
| `0x07` | `WB_U_VOICE_ABORT` | 无 |
| `0x08` | `WB_U_SEND` | `convIndex, mode8`（`0=转写完直接发`、`1=先回显等确认`） |
| `0x09` | `WB_U_CONFIRM` | `convIndex` —— 确认投递 PC 缓存的转写文本 |

协议常量定义见 `firmware/main/wb_proto.h`，与 `bridge/wb_bridge.py` 顶部的常量表**一一对应**，改一边必须同步另一边。

---

## HTTP 调试端点

中转服务在 `127.0.0.1:8765` 上开了一组只读端点，方便不开设备也能查状态：

| 端点 | 用途 |
|---|---|
| `/health` | 服务健康 + ASR 模型状态 |
| `/ble` | 设备连接状态（`connected` / `mtu` / `pushed` / `received` / `voice_bytes`） |
| `/gw` | WorkBuddy 网关状态（端口 / 缓存） |
| `/status` | WorkBuddy 当前状态 |
| `/conversations` | 对话列表 |
| `/messages?id=<sid>` | 指定会话的消息 |
| `/usage` | 额度用量 |
| `/send?text=&conv=&sender=&wait=` | **跳过 ASR 直接发一条**（走完整投递链路 + 落库验真） |
| `/route?sid=<会话id>` | **投递路由诊断**：这个会话会被投到哪个实例、凭什么这么判 |
| `/sim?text=&idx=` | **走工牌同一条链路**投递（含等空闲/重试/回执），用于不开麦克风验收 |
| `/push` | 手动全量下发一次 |

---

## 几个踩过的坑

这部分可能比代码本身对你有用。

### 1. BLE 带宽撑不住原始 PCM

BLE 实测约 **10 KB/s**，而 8kHz / 16bit 单声道 PCM 要 **16 KB/s** —— 必然丢包，而且固件里是静默丢帧，表现成「录了一半没声音」。

解法是 **IMA/DVI 4bit ADPCM**：8kHz 直接压到 4 KB/s，留出 3 倍余量。

每个包用**上一包的尾采样做 seed 重置预测器**，所以包与包之间零耦合 —— 丢一包只影响那一包，不会误差累积（`bridge/test_adpcm.py` 里有 Python 复刻的往返测试，类语音信号 SNR ≈ 16.9 dB，丢包不劣化）。

### 2. 一录音就重启

现象：按 OK 开录，1 秒内必复位。

根因在 `wb_ble_voice_push()`：缓冲按 `WB_FRAME_MAX 192` 分配，但块长却是**按 MTU 247 算的 238 字节**。`memcpy` 冲出 50 字节，砸掉了栈上的返回地址，取指跳到 `0xfffe` → 异常复位。

修法是缓冲扩到 264（对齐 MTU 247 的整包上限）+ 块长做双重夹紧。

### 3. ASR 一跑就崩

两个独立的坑：

- **PyAV 19 删掉了 `metadata_errors` 参数**，而 faster-whisper 1.2.1 还在传 → 直接抛异常。绕开 PyAV，用 `wave` + `numpy` 自己解码、插值到 16kHz。
- **ctranslate2 在子线程加载模型会 access violation**。Bleak 的回调和 HTTP 服务各跑在自己的线程里，模型必须**在主线程预加载**完成后再放行。

### 4. 怎么把消息"发进" WorkBuddy

WorkBuddy 桌面端自带一个本地 HTTP 网关（就是 `codebuddy --serve` 那套）。**每个窗口一个实例，端口随机**：

```
POST /api/v1/sessions/{id}/reply   向"该实例当前装载的会话"投递一条用户消息
POST /api/v1/runs                  发起一次后台 Agent 运行（没有 sessionId 参数）
GET  /api/v1/sessions/live         该实例当前 live 会话 {sessionId, writerOccupied}
GET  /api/v1/sessions              该实例工作目录下的会话列表
GET  /api/v1/status                该实例运行状态 {busy, runStatus, activeSessionId}
GET  /api/v1/info                  该实例信息（**含 cwd**，实例身份的硬标识）
```

认证用的密钥由 GUI 进程**随机生成、只通过环境变量注入子进程、不落盘**。所以直连 CLI 会卡在 `AuthenticationRequiredError`。

这里的做法是（`bridge/gw_discover.py`，跨平台）：
1. 读活进程的环境块，取出 `CODEBUDDY_GATEWAY_PASSWORD`：
   - **Windows**：PEB 遍历（NtQueryInformationProcess，无需其他依赖）；
   - **macOS**：`ps -Eww -p <pid>`（同用户进程免 root 可读）；
   - **Linux**：`/proc/<pid>/environ`（同用户可读）。
2. 无认证并发扫本地端口，靠**唯一返回 401 `AUTH_REQUIRED`** 的特征认出网关（不会消耗配额）。

冷发现 ~1.5s，有缓存后 ~0.1s。密钥只在内存里，不写盘（端口/密钥缓存文件含密钥，权限 0600，路径见 `.gw_cache.json`）。

⚠️ 限流说明：网关的令牌桶（2 次/分、12 次/时）**只对认证失败计数**（防爆破），成功的认证请求不消耗配额 —— 所以"挨个实例试一遍"不会把自己限流死。

### 5. "显示 SENT，电脑却没反应" —— 投递为什么这么难

这条链路上有两个互相独立的坑，都踩过：

**坑一：reply 只服务"它自己装载着的那一个会话"。**

逆向网关源码（`app.asar` 内嵌 OpenAPI + sessions 控制器）拿到的权威语义：

```js
async replySession(id, body) {
  const cur = this.sessionManager.sessionSubject.value;   // 本实例装载的会话
  if (!canDeliverFollowReply(cur?.id, id) || !cur)        // 即 cur.id === id
     409 SESSION_FOLLOW_NOT_LIVE;
  await this.agentService.runDefault(buildFollowReplyItems(text, cur.id), ...);
}
```

发到别的实例一律 409。**好消息是 409 很干净** —— 消息不会落到任何别的地方。所以"挨个实例试 + 看谁受理"是安全且完备的策略。

**坑二：会话正在跑 run 时，reply 返回 `delivered:true` 但消息被静默丢弃。**

所以只信回执必然吞掉假成功。必须投完**回读会话记录（`projects/*/<sid>.jsonl`）验真**，只认新增的 `type=message & role=user` 行。

**为什么以前十投九败**：老的解析链是 `live → owner → 心跳`，全是**运行时状态**，而工牌按 OK 绝大多数发生在会话**空闲**期 —— 那一刻所有实例的 `/sessions/live` 和 `/status.activeSessionId` 都可能为空，于是路由必然落空，报 `Conv not in live window`。

现在的做法（`gw_discover.deliver_candidates`，5 条互相独立的键，按可信度排序）：

| 优先级 | 判定键 | 说明 |
|---|---|---|
| 0 | 心跳文件 endpoint **且** owner 一致 | 心跳里写死了 `sessionId → endpoint`，最硬 |
| 1 | `/info.cwd` == 会话 cwd **且** owner 一致 | 两个独立来源同时确认 |
| 2 | `/info.cwd` == 会话 cwd | **与忙闲无关**，真正解决问题的一条 |
| 3 | `/status.activeSessionId` == sid | 运行时归属 |
| 4 | `/sessions/live.sessionId` == sid | writer 附着时才非空 |
| 5/6 | 会话列表里含 sid / 其余实例 | 兜底，靠 409 过滤 |

其中「`/info.cwd` ↔ 会话 cwd」是关键：`/api/v1/info` 返回该实例的工作目录，会话心跳文件里写着同一个 cwd，两边一对就把"会话"钉在了"实例"上 —— 跟会话忙不忙、writer 在不在毫无关系。

投递流程（`wb_bridge.deliver_text`）：**等空闲 → 锁实例投递 → 回读验真 → 没落库就重来**，最长等 `_BUSY_WAIT_MAX`（240s）。投递在独立线程里跑（`_deliver_async`），不会卡住 BLE 事件线程。

排障三件套：

```bash
curl "http://127.0.0.1:8765/route?sid=<会话id>"     # 这个会话会被投到哪个实例、凭什么
curl "http://127.0.0.1:8765/send?text=hi&conv=<id>" # 走完整投递链路（跳过 ASR）
python session_tail.py <会话id> 20                   # 回读会话记录，看消息到底落库没有
python live_map.py <会话id前缀>                      # 现场全景：心跳/实例/归属
```


---

## 已知限制

- **ASR 精度**：8kHz + 4bit ADPCM 的链路本身就丢了不少高频（中文擦音尤其吃亏），叠加 `small` 模型时短句容易糊。**换 `medium` / `large-v3` + CUDA 能明显改善**，这是目前性价比最高的提升手段。
- **麦克风增益偏高**：实测 `peak=32768`、有削顶。凑近说话可用，远场会明显劣化。
- **单设备**：中转服务同一时刻只服务一台工牌。
- **Windows / macOS / Linux 均可**：网关发现已跨平台（Windows PEB 遍历、macOS `ps -Eww`、Linux `/proc/environ`）。Windows 端功能最全（原始开发平台）；macOS 上只读功能与发送均已实测可用。

---

## 目录结构

```
ai-passport-workbuddy/
├── bridge/                      PC 侧中转服务
│   ├── wb_bridge.py             主服务：BLE 收发 + HTTP + ASR + 发送
│   ├── gw_discover.py           WorkBuddy 网关发现（PEB 读密钥 + 端口扫描）
│   ├── monitor.py               串口日志监听（带断线重连）
│   ├── test_adpcm.py            ADPCM 编解码往返测试
│   ├── gen_charset.py           生成字库字符集
│   ├── check_font_coverage.py   检查字库对真实对话的覆盖率
│   ├── scan.py / serial_listen.py
│   └── requirements.txt
├── firmware/                    相对 FoloToy 上游的改动（覆盖式）
│   ├── main/
│   │   ├── wb_proto.h           ★ 协议定义，与 bridge 常量表对应
│   │   ├── wb_ble.c/.h          BLE 收发
│   │   ├── wb_voice.c/.h        录音任务
│   │   ├── wb_adpcm.c/.h        IMA ADPCM 编码
│   │   ├── wb_app.c/.h          ★ LVGL UI（状态灯 / 列表 / 气泡 / 录音）
│   │   ├── wb_fonts.h           中文字体声明
│   │   ├── demo_workbuddy.c     入口
│   │   ├── CMakeLists.txt / main.c / demo.h
│   │   └── fonts/               生成好的字库（lv_font_wb_14 / 16）
│   ├── fonts/                   gen_font.js + symbols.txt
│   └── sdkconfig.defaults
└── docs/                        设计文档
```

---

## 致谢

- 硬件与基础固件来自 **[FoloToy / ai-passport](https://github.com/FoloToy/ai-passport)**（MIT）。
- 中文 UI 用思源黑体（Source Han Sans SC）生成子集字库。

## License

MIT —— 见 [LICENSE](LICENSE)。上游版权归 FoloToy 所有，本仓库新增部分同样以 MIT 释出。
