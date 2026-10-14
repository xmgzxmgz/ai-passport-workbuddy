# AI Passport（FoloToy）开源可穿戴 AI 工牌 — 硬件与社区二创整理

> 整理时间：2026-10-14
> 结论先行：这不是"AI 给 Agent 发护照"的协议类项目，而是一块 **99 元的开源可穿戴 AI 硬件开发板**——出厂是电子工牌，刷不同固件就变成掌机、宠物机、效率工具。它的真正价值在**开放固件生态 + AI Coding 辅助开发**，让非嵌入式开发者也能改硬件。

---

## 一、这是什么

| 项 | 内容 |
|---|---|
| 产品全名 | **FoloToy AI Passport**（"The Open Wearable AI Passport"） |
| 厂商 | FoloToy（AI 陪伴玩具公司，github.com/FoloToy） |
| 身份 | 与 **TRAE**（字节 AI IDE）联名，TRAE 创造力大赛随赠身份牌 |
| 定位 | Open Wearable AI Agent —— 可穿戴、可安装固件、可被用户重新定义 |
| 官方口号 | WEAR · PLAY · CREATE（戴起来 / 玩固件 / 自己造） |
| 价格 | 官方 199 元，早鸟/券后 **99 元** |
| 开源协议 | **MIT**（固件开源） |
| 火爆程度 | 咸鱼炒到 300+；官方淘宝现货售罄转预售；社区已沉淀 **236 个玩法** |

**三层玩法结构：**
1. **WEAR** — 当身份卡用：手机 BLE 同步姓名/头像/简介/图片
2. **PLAY** — 浏览器里一键刷官方固件，且保留名片功能
3. **CREATE** — 自己写固件，或让 AI Coding Agent 帮你写

---

## 二、硬件规格（实测核对过）

| 模块 | 规格 | 说明 |
|---|---|---|
| MCU | **ESP32-C3**，RISC-V 单核 160MHz | 乐鑫芯片 |
| Flash | **8 MB** | 3MB 给 factory-app 分区 |
| RAM/PSRAM | **无 PSRAM**，可用内存约 370KB（free 约 170KB） | **最大硬伤**，跑不了 MicroPython 大程序 |
| 屏幕 | ST7789P3 TFT，**240×320** 竖屏，RGB565，SPI2 40MHz | 无触摸、无 TE |
| 按键 | 3 个物理键 UP / DOWN / OK | 共用 GPIO0 ADC 电阻梯 |
| 音频 | **ES8311** codec，麦克风 + 扬声器，I2S 全双工 | 支持录音与播放 |
| 电池 | CW2017 电量计 + 520mAh 锂电 | 电量读取精度一般 |
| NFC | **NTAG213 被动标签** | ⚠️ 不连 MCU，只能被手机碰读，读不了外部卡 |
| 无线 | Wi-Fi 2.4GHz + **BLE 5**（NimBLE） | 不支持蓝牙经典 |
| 接口 | USB Type-C 2.0（USB Serial/JTAG，GPIO18/19） | 免驱刷机 |
| 尺寸 | 60 × 95 × 8.5 mm，约 **50 g** | 透明外壳，主板外露 |
| 其他 | 独立硬件电源键（区别于三个功能键） | |

**已知限制（上车前必读）：**
- **同一时刻只能装一个固件**——刷了新的，旧的玩法就没了（有第三方 meta-pass 项目可解决，见下文）
- 无 PSRAM → 复杂应用、MicroPython 受限
- 无扩展接口 → 外接摄像头/读卡器基本无望
- 板载 NFC 是被动标签，不是读卡器

---

## 三、开发方式（门槛极低，这是核心卖点）

| 方式 | 说明 |
|---|---|
| 官方基线仓库 | `github.com/FoloToy/ai-passport`（main 分支 = 最小可运行基线） |
| 开发框架 | **ESP-IDF 5.5.x** + LVGL |
| 板级隔离 | `components/bsp/` 暴露稳定 API；应用逻辑写在 `main` |
| AI 协作约定 | 仓库内置 `AGENTS.md`、`docs/development/ai-guide.md`、`docs/hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md` |
| 网页刷机 | Web Serial 一键烧录，**不用装驱动、不用命令行、不用重启** |
| 官方刷机页 | https://ai-passport.folotoy.cn/tools/web-flasher/ |
| 玩法社区 | https://ai-passport.folotoy.cn/plays/ |
| 配置小程序 | 微信搜 "FoloToy AI Passport" |

**关键创新点**：扫描设备背面二维码 → 打开网页 → 复制预设提示词丢给 AI Coding Agent（Codex / Claude Code / TRAE）说"我想让它变成一个……"，剩下的代码修改、构建、调试由 Agent 完成。**这让嵌入式开发变成了"说人话"。**

---

## 四、有意思的二创项目（按类别）

### 🎯 社交与身份
| 项目 | 作者 | 做了什么 |
|---|---|---|
| **SparkLink** | AB | 两块 Passport 连接后，AI 读双方 Persona（在做的事/技能/找的问题），找出**两人自己都没意识到的隐藏连接**，同亮两面屏幕。设计哲学：AI 只帮你找到第一句话，然后**退场**。技术难点：8MB Flash 下把 UI 压成 6 个状态屏、后端精简到 5 个 API |
| **Passport Studio** | — | 把名片、图片二维码、电台、AI 对话整合进同一导航。**社区活动玩法下载排名第一**。回答了一个设计问题：改造之后，设备还能不能"回到自己" |

### 🎮 游戏
| 项目 | 仓库/作者 | 简介 |
|---|---|---|
| 三键俄罗斯方块 | `FoloToy/ai-passport` demo/tetris-game | 官方 demo，实时游戏循环 + 音效，仅 0.73MB |
| 《牛来快跑》 | `Regan-Lu/niulai-ai-passport-firmware` | 烧录固件版独立游戏，"牛来"梗 |
| Game ECHO | `WhiteMagic2014/passport_game_echo` | 游戏项目 |
| 语音飞行 | `account-w/Voice-Flight` | **用麦克风吹气/喊声控制升降**的障碍飞行游戏 |
| BLE 寻宝 | `azzotest/ai-passport-treasure-hunt` | 爸爸为儿子做的亲子寻宝，BLE 通信藏宝/寻宝 |
| 宝可梦图鉴 | `punkcatl/ai_passport_pokedex` | 15 只宝可梦动漫风图鉴（RGB565 预渲染） |

### 🐾 虚拟宠物 / 养成
| 项目 | 仓库 | 简介 |
|---|---|---|
| Basti 像素猫 | `LeopardDennis/ai-passport-basti` | 可喂养、玩耍、陪伴成长 |
| 学舌宠物 | `Ecparterhacs/talking-pet-ai-passport` | 用俏皮声音复述你说话 |
| 声音精灵养成 | `Ritten1/FoloToyAIPassport` | **捕捉现实声音，AI 把声音变成可收藏培养的像素精灵** |
| Aemeath 虚拟宠物 | `linn0x/ai-passprot-aemeath` | 离线养成 |
| Volt Pet | `popoer/ai-passport-volt-pet` | 宠物心情**跟随你的真实电量** |
| PDK Pass F1 伴侣 | `LeopardDennis/ai-passport-pdkpass` | 赛道日历 + 车手榜 + 分站详情 + 时区自动切换 |
| 五月天卜卜养成 | `chenjie1129/MayDayFansInTraePassport` | 粉丝向养成 |

### 📚 学习 / 教育
| 项目 | 仓库 | 简介 |
|---|---|---|
| 词根闪卡 | `joeseesun/vocab-passport` | 词根背单词 + 发音，纯离线 |
| 单词熊 | `arraylee/ai-passport-word-bear` | 离线单词学习 |
| 唐诗欣赏 | `fyx2019/ai-passport-tang-poetry` | 10 首经典唐诗，支持翻页 |
| 课程表 + GPA 助手 | `elaemc0209/folotoy-ai-passport` | 学生日程 + 成绩管理 |
| 摩尔斯码训练 | `PhoenixZHC/folotoy_morse_trainer_blank` | 离线摩尔斯码输入训练 |

### 🧘 工具 / 解压
| 项目 | 作者 | 简介 |
|---|---|---|
| **Focus Pass** | — | **反 App 的实体番茄钟**。按 OK 开始 25 分钟倒计时，结束响铃进休息。Game Boy 像素风（出于内存优化，非纯审美）。证明了 Passport 能成为"每天都会用"的桌面工具 |
| 口袋答案之书 | — | 离线纠结小顾问，按键随机给指引，分鼓励/理性/随缘三类文案，NVS 持久化收藏 |
| 敲木鱼 | `nistudyc/trae-passport-muyu` | 离线功德计数器 |
| 双色球选号 | `PhoenixZHC/folotoy_shuangseqiu` | 随机选号 |
| 随身塔罗命理机 | 社区 | 塔罗占卜 |
| 番茄钟 / 天气时钟 / 音乐盒 | 社区 | 常规效率件 |

### 🔧 开发工具类
| 项目 | 仓库 | 简介 |
|---|---|---|
| **Claude Buddy** | `FoloToy/ai-passport` demo/claude-buddy-port | **和 Claude Code 配对**：Claude Code 要写文件时，工牌亮出 `CLAUDE NEEDS APPROVAL`，按实体键放行，文件真的建出来。**把 AI 工牌变成 AI 编程助手的物理遥控器** |
| **meta-pass** | `alexwwang/meta-pass` | **把单固件设备变成多固件设备**（解决最大痛点）。手机扫码 LAN 传固件，SHA-256 校验，带启动器可切换 slot，开机选槽位 |

---

## 五、我的判断：它到底有意思在哪

**1. "不做什么"比"能做什么"更稀缺。**
8MB Flash、400KB RAM 的硬件天然拒绝功能堆砌，迫使创作者回到最本质的问题：这东西到底为谁解决什么？SparkLink 让 AI 帮人少聊 AI，Focus Pass 把工具从手机拽回桌面——都是"做减法"的产物。

**2. 硬件开发门槛被 AI 打平。**
过去改硬件要会嵌入式，现在扫个码、丢个提示词给 AI Agent 就能改。这是"AI 改变智能硬件开发方式"的一个具体样本。

**3. 便宜到可以浪费。**
99 元买的是：透明外壳的颜值、可自定义的身份显示、以及**"挂脖子上别人一看就知道是同行"的社交筛选器**。有拆解帖说得直白——387 赞的那条。

**4. 局限同样真实。**
单固件限制、无 PSRAM、无扩展口、被动 NFC——评论区画风完全是掌机魔改党在挑刺（"换成墨水屏/反射屏就好了，LCD 功耗高"）。

---

## 六、如果你想上手

**低成本试水路径：**
1. 买一块（99 元，注意现货/预售状态）
2. 网页刷机试几个官方玩法，熟悉流程
3. 挑一个自己**每天真的会用**的场景，做减法而非加法
4. 扫背面二维码 → 复制提示词 → 丢给 AI Coding Agent → 跑通一个小功能
5. 再发帖

**值得直接参考的仓库：**
- 官方基线：`github.com/FoloToy/ai-passport`
- 多固件方案：`github.com/alexwwang/meta-pass`
- 玩法合集：`cnb.cool/sshnuke.club/aipassport`
- 官方玩法页：`ai-passport.folotoy.cn/plays/`

---

## 附：信息源

| 来源 | 链接 |
|---|---|
| 官方产品站 | https://ai-passport.folotoy.cn/ |
| 官方 GitHub | https://github.com/FoloToy/ai-passport |
| 玩法合集 | https://cnb.cool/sshnuke.club/aipassport |
| 二创解读（TRAE 论坛） | https://forum.trae.cn/t/topic/181757 |
| 设备全解析（TRAE 论坛） | https://forum.trae.cn/t/topic/180446 |
| 深度评测（什么值得买） | https://post.smzdm.com/p/ak8k74zk/ |
| meta-pass 多固件方案 | https://github.com/alexwwang/meta-pass |
