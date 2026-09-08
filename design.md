# 续火花助手（Windows 本地自用版）技术设计

## 1. 设计原则

- 净室重写：只依据已确认需求和实时网页行为实现，不复制同类项目源码、结构、选择器、文案或资源。
- 先确保不会误发，再提高成功率。
- 所有发送入口复用一个执行服务；桌面程序和 worker 不各自实现发送逻辑。
- 发送触发后不自动重试。
- 页面缺失与登录失效必须由证据区分。
- 默认不采集页面正文、截图或 Trace。

## 2. 技术栈

- Python 3.12+
- Playwright 异步 API + Chromium
- PySide6 / Qt Widgets 桌面界面（使用 PySide6-Essentials）
- SQLite（Python 标准库 sqlite3）
- Windows DPAPI（CryptProtectData/CryptUnprotectData）
- Windows 命名互斥量（CreateMutexW）
- Windows Task Scheduler（schtasks + 由程序生成的 XML）
- Windows Toast 通知；通知实现失败时写入本地事件并返回明确错误
- pytest + pytest-asyncio

源码开发仍直接运行；Windows 版使用 PyInstaller `onedir` 生成程序目录，3.0.2 起以用户级 Inno Setup 安装 EXE 为推荐分发方式，保留便携 ZIP；不制作需要管理员权限的安装程序。

正式版本从 3.0.0 开始，以 `src/spark_keeper/__init__.py` 的 `__version__` 为唯一来源，hatch 动态读取。安装器文件名为 `SparkKeeper-版本号-Setup.exe`，备用 ZIP 为 `SparkKeeper-版本号-win64.zip`；ZIP 内部固定 `SparkKeeper/` 加根目录 `更新.cmd`/`update.ps1`，由用户主动离线完整替换便携目录。GitHub 发布工具默认仅预检，显式授权才建立草稿并上传校验通过的安装 EXE、ZIP、各自 SHA-256 文件及 Qt 对应源码，不修改历史标签与资产。

## 3. 目录设计

```text
requirements.md
 design.md
 tasks.md
 pyproject.toml
 src/spark_keeper/
   __init__.py
   __main__.py
   paths.py
   models.py
   database.py
   dpapi.py
   mutex.py
   logging_safe.py
   notifications.py
   scheduler.py
   automation/
     __init__.py
     errors.py
     browser.py
     douyin_chat.py
     send_state.py
     spark_scan.py
     service.py
   ui/
     __init__.py
     app.py
     theme.py
   worker.py
 tests/
tools/
  build_portable.py
  portable_entry.py
 work/
 outputs/
```

源码运行时，`work` 保存项目内临时内容，运行数据位于 `work/local-data`。冻结后的便携自用版以可执行文件目录作为程序根目录，数据库、DPAPI 登录态、任务 XML 和本地输出全部写入当前用户 `%LOCALAPPDATA%\SparkKeeper`；发行目录保持只读且不携带制作者运行数据。

## 4. 进程结构

### 4.1 桌面进程

负责：

- 展示和修改配置。
- 启动扫码登录。
- 搜索并确认好友。
- 启动验证配置。
- 显示整批预览并启动手动发送。
- 管理计划任务。
- 展示历史、日志和待处理操作。
- 主界面采用左侧原生 `QPushButton` 导航（任务台、好友管理、发送历史、运行日志）和右侧 `QStackedWidget` 页面栈。非活动页由 Qt 管理可见性和焦点，不再手动移动到屏外。选中背景为 `#bdd2f5`，选中悬停为 `#adc7f0`，深蓝文字和固定宽度左侧标记区分状态，不改变字体度量或造成位移。
- 主界面共用 `ui/theme.py`：使用 Qt 调色板、字体和 QSS 统一浅蓝色板、圆角导航、页面标题、卡片、状态徽章、操作层级、表格斑马纹和空状态。卡片圆角约 14px，按钮和输入区域约 8px；界面由 Qt 缓冲绘制，不混用 Tk 控件，不使用截图遮罩或原生重绘补丁。
- 默认窗口为 `1120x780`，最小窗口为 `940x650`；卡片说明自动换行，宽表格使用 Qt 原生按需滚动条，最小尺寸仍保留操作入口、进度区域和状态栏。“本次进度”仅保留卡片外框，内部表格不重复加框；其他表格保留轻边框。
- 账号动作按状态互斥显示：未登录仅扫码登录，已登录仅清除登录；忙碌期间禁用。启用控件保留 `QCheckBox` 的鼠标和键盘语义，采用约 12 逻辑像素的白色空心/黑色实心圆形指示器，不缩小文字点击区域。
- 切页先显示已保留的页面，再后台读取好友、历史或日志；每页最多一个读取任务，通过访问世代与刷新版本丢弃过期结果。表格按稳定行标识增量更新，保留未保存编辑、选择、焦点及滚动位置，不在切页时整表清空重建。
- 已保存好友以原生 `allColumnsShowFocus` 显示整行焦点，支持 Ctrl/Shift 多选及选择计数；操作栏使用“全选/取消选择”切换按钮和上下文菜单，按选择数量与结构化启用状态刷新可见动作，并统一遵守忙碌/关闭状态。启停与确认后的删除在数据库中按整组选中 ID 原子执行；任一成员失败回滚整组，有历史的删除对象仅停用并保留记录。
- GUI 构造业务窗口前，以当前用户 SID/会话稳定身份获取独立互斥量并建立限当前用户的 Qt 本地管道。第二实例只请求激活并退出，不触碰数据库；有限等待未响应时不另开窗口。真正退出才释放，忙碌拒绝关闭期间继续持有。与原有自动化互斥量分离，`--smoke` 显式隔离诊断。

Playwright 操作在后台线程中运行，结果通过线程安全队列回传 Qt 主线程，由 `QTimer` 消费；窗口关闭后停止定时器并忽略迟到结果。

### 4.2 worker 进程

由 Windows 计划任务启动：

```text
python -m spark_keeper.worker --scheduled
```

worker 只调用共享 `BatchService`，不能直接调用页面发送函数，也不能开启人工重复覆盖。

### 4.3 共享执行服务

`BatchService` 是配置验证、手动发送、定时发送和错过补发的统一入口：

1. 获取 Windows 命名互斥量。
2. 加载计划、账号和启用目标快照。
3. 创建批次记录。
4. 恢复 DPAPI 登录状态。
5. 创建浏览器会话。
6. 检查页面、登录和目标身份。
7. 逐个执行目标。
8. 更新尝试、批次和事件。
9. 关闭浏览器并释放互斥量。
10. worker 模式发送 Windows 通知。

## 5. SQLite 数据模型

### `app_meta`

键值设置，包括 schema 版本、页面适配器、最后一次错过检查日期。

### `account`

单行账号公开信息：

- `platform_user_id`
- `display_name`
- `logged_in_at`

不保存 Cookie 或 Storage State。

### `targets`

- 本地 ID
- 稳定身份键（唯一）
- 昵称
- 抖音号（如可见）
- 个人主页地址（如可见）
- 搜索关键词
- 头像地址（可选，仅辅助确认）
- 身份证据 JSON
- 是否启用
- 确认时间

好友记录和同时启用好友均不设应用层固定数量上限，不新增数量上限配置。确认同一稳定身份键时更新并启用原记录，不重复创建目标；停用与重新启用不改变目标身份。

火花批量导入与单个用户确认保存不同：`import_spark_contacts` 在一次 `BEGIN IMMEDIATE` 中核对扫描账号及登录时间，对已有稳定键/同主页/同抖音号的记录保持不变，只插入停用的新目标。任何歧义或写入异常整体回滚；不按名称合并身份。火花观察时间和当时状态保存在身份依据中，不表示状态永久有效。

### `plan`

单行计划：

- 是否启用
- `HH:MM` 本地执行时间
- 消息类型 `text` / `spark_sticker`；完整文本仅用于文本模式，原生模式规范为空
- 计划确认时间
- 创建/修改时间
- 好友间随机等待最短秒数
- 好友间随机等待最长秒数

### `batch_runs`

- UUID
- 模式：`validation/manual/scheduled/missed`
- 计划日期时间
- 开始和结束时间
- 状态
- 安全错误代码
- 目标数量和结果计数

### `send_attempts`

- UUID
- 批次 UUID
- 账号身份键
- 目标 ID
- 当前本地日期
- 完整消息文本
- 状态：`sending/success/failed/unknown/duplicate/cancelled`
- 是否人工覆盖
- 被覆盖的原尝试
- 开始、触发发送和结束时间
- 安全错误代码

### `daily_send_guards`

唯一键：

```text
(account_key, target_id, local_date)
```

只为 `sending/success/unknown` 保留。明确失败不会形成当天防重复依据。进程启动时，残留的 `sending` 原子转换为 `unknown`。

### `pending_actions`

保存登录失效、人工验证、结果不确定和错过计划等需要桌面程序处理的事项。

`unknown_send` 按当前待处理快照合并为一次提醒；模态期间阻止重入，其他模态或任务忙碌时延后。关闭提示仅将已展示提醒标记为已知晓，不更改发送 `unknown`、历史或当天防重复依据；弹窗期间新到达的提醒留待下一次展示。空队列继续低频检查，已知晓事项重启后不重复提醒。

### `events`

按用户要求保存事件原文。目标标签包含真实名称和本地 ID；错误保留类型、原因链、诊断备注和调用栈，不再匿名化或截断。凭据仍由 DPAPI 保护，不主动转储登录态、请求头或聊天正文。

初始化以规范数据路径对应的短期维护锁串行执行，先只读预检 schema；更高或无效版本明确拒绝。仅需迁移时使用 SQLite backup API 将已提交 WAL 一并备份到数据目录上级 `backups/`，备份失败不执行迁移，DDL 与版本标记在统一事务中提交。SQLite 只读 WAL 访问可更新易失共享内存读取标记，不修改主库或 WAL 的业务内容。启动恢复仅在成功获取自动化锁后进行，避免更改其他实例正在发送的记录。

所有写入使用显式事务；启用 WAL、外键和 busy timeout。

## 6. DPAPI

- `auth-state.bin` 保存 DPAPI 加密后的 Playwright Storage State JSON。
- 使用当前 Windows 用户作用域，不使用 `CRYPTPROTECT_LOCAL_MACHINE`。
- 使用应用固定 entropy，避免其他无关程序直接调用 DPAPI 解开同一数据块。
- 文件以原子替换方式写入。
- 解密失败视为登录状态不可用，不回退到明文文件。
- 日志不得输出原始 JSON、Cookie 名值、请求凭据或 DPAPI 错误数据。

浏览器自动任务采用临时 BrowserContext：

1. 解密 Storage State 到内存。
2. 将对象直接传给 Playwright `new_context(storage_state=...)`。
3. 不创建持久 Chromium 用户目录。
4. 任务结束关闭 context；不把解密内容写入临时 JSON。

扫码登录完成后从 context 读取 Storage State，在内存序列化并立即 DPAPI 加密保存。


## 7. 跨进程互斥

使用固定的 Local 命名互斥量。范围覆盖整个浏览器任务，而不是单条消息。

- 获取失败返回 `already_running`。
- 不等待并静默排队。
- Windows 在进程退出时自动释放句柄。
- Task Scheduler 的 `MultipleInstancesPolicy` 同时设为 `IgnoreNew`，作为第二层保护。

## 8. 页面适配器

首选 `https://www.douyin.com/chat`。可行性实验失败后才人工切换到 Creator Center；首版不在运行时自动降级。

页面适配器只暴露语义操作：

- `open_chat()`
- `classify_page()`
- `search_targets(query)`
- `capture_current_chat_candidate(expected_name)`
- `open_confirmed_target(identity)`
- `verify_recipient(identity)`
- `get_composer()`
- `send_text_and_confirm(text)`

定位优先级：

1. 可访问性角色、标签和稳定 `data-*` 属性。
2. 明确的输入框语义和可见文本。
3. 经本项目实时观察确认的最小页面结构。

不导入同类项目的选择器集合。所有候选必须限制在可见元素和当前搜索结果容器中。

## 9. 好友身份确认

搜索结果转换为 `FriendCandidate`：

- `stable_key`
- `display_name`
- `douyin_id`
- `profile_url`
- `avatar_url`
- `evidence`

当聊天搜索只覆盖已有会话时，桌面程序提供用户辅助捕获流程：

1. 使用 DPAPI 保存状态打开应用独立的可见 BrowserContext。
2. 用户按备注名或昵称手动搜索并打开正确好友聊天；程序输入项填写右侧聊天顶部实际显示的名称。
3. 用户回到桌面程序点击“读取当前聊天”。
4. 页面适配器只检查可见聊天标题、主页链接、会话标识和头像，不读取聊天正文。
5. 捕获过程不访问编辑器内容、不输入文本、不触发发送。
6. 候选返回桌面程序后仍需用户再次核对并确认保存。

该流程不复用日常浏览器账号，也不把抖音号视为可靠的全站搜索入口。

规则：

- 完全相同昵称仍可能有多个候选，必须让用户选择。
- 不使用前缀匹配自动选择。
- 群聊候选被排除。
- 标题含纯数字中西文括号成员数后缀（如 `好友（7）`）也按群聊排除。
- 发送前重新搜索稳定身份键并打开。
- 聊天打开后再次比对可见标题和身份信息。
- 身份证据不足或不一致时返回 `target_ambiguous` 或 `target_identity_mismatch`，禁止输入文本。

### 9.1 火花只读扫描

- `automation/spark_scan.py` 遍历当前左侧聊天列表，逐段滚动并处理虚拟列表复用；稳定会话标识的摘要仅用于去重，不升级为可发送身份。
- 真实网页适配使用已授权观察确认的 `conversationConversationListwrapper`、`conversationConversationItemwrapper` 与专用 `commonStreak` 徽章区域，不读取消息预览正文。
- 只读取与该行绑定的渲染会话白名单字段。公开 IM SDK 的类型 `1` 为单聊，`2` 为群聊，其他类型不能自动导入。
- 火花依据为 `coreInfo.ext['a:consecutive_chat_data'].flame_infos` 中有效时间窗的状态；`LIGHT=1` 和 `GRAY=2` 均为有效，`RECOVER=3` 映射为独立的待恢复状态，`LIGHT_DOWN=4` 为非有效。不以正常图标或正整数天数替代该状态判断。
- 对方 `participant.sec_uid` 映射到规范个人主页，扫描、搜索候选和当前聊天身份复核共用同一提取规则。扫描导入目标无法取得强身份复核时，拒绝发送，不回退为仅昵称匹配。
- 有明确末端证据才返回 `complete`；滚动停滞、读取中断、数量/轮数/时间上限均返回 `partial`，取消保留先前结果。默认安全边界为 1000 项、160 轮、90 秒。
- 结果只在内存中供用户预览；导入前重新核对账号。默认选中身份可靠的新有效火花（含GRAY）和RECOVER对象；所有行可自行勾选，其他火花状态也可人工导入可靠单聊。任一所选对象身份不可靠/冲突或为群聊时明确拒绝整批，不静默忽略。全选作用于当前可见列表，新增目标统一停用。
- 公开依据：抖音 PC IM `__federation_expose_default_export.0ccd0cf4.js` 的火花状态分支和 `4187.2096e141.js` 的会话类型枚举。网页结构变化时保留未知状态并停止不可靠扫描，不伪造完整结果。

## 10. 页面状态分类

按证据优先级分类：

1. 明确安全验证标记 → `human_verification_required`
2. 明确登录页面标记 → `authentication_required`
3. 聊天页面关键控件存在 → ready
4. URL 正确但控件迟迟未出现 → `page_not_ready`
5. 页面基本结构发生变化 → `page_structure_changed`

页面加载和纯读取可做少量、固定次数的重新等待或刷新；发送动作不重试。

安全诊断只读取白名单属性、可见控件数量、页面标题及移除查询参数后的 URL，不读取页面正文和 HTML。

## 11. 发送状态机

```text
PREPARED
  └─ SQLite 预留成功 → RESERVED
       └─ 触发发送 → TRIGGERED
            ├─ 新己方气泡与文本或原生资源匹配 → OBSERVING
            │    ├─ 明确失败标记 → FAILED
            │    ├─ 发送中 → PENDING
            │    │    ├─ 明确失败标记 → FAILED
            │    │    └─ 发送中消失且稳定 → SUCCESS
            │    └─ 全程稳定且无失败 → SUCCESS
            └─ 超时/页面丢失/进程异常 → UNKNOWN
```

- 发送前记录旧的最新己方消息身份或内容摘要。
- 文本输入后验证编辑器内容与期望文本等价；原生“续火花”不输入文本，点击准确表情项即触发一次发送。
- 只确认新产生且内容匹配的己方消息。
- 新气泡刚出现时不是成功，必须观察发送中和失败标记的终态。
- `TRIGGERED` 后所有无法证明失败的异常都按 `unknown` 处理。
- 不自动重试。
- `plan` 与 `send_attempts` 保存 `message_kind`；schema v3 将旧行确定为 `text`。补跑冻结类型和内容，旧 JSON 快照缺少类型时按文本处理；改变类型不改变当天去重键。
- 原生资源依据当前网页互动表情配置：名称“续火花”，稳定路径 `/obj/im-resource/1687263281313-ts-e7bbade781abe88ab12e706e67`。签名域名和参数不固定；它不同于小表情“[续火花吧]”。
- 原生发送必须先核对唯一可见资源、当前聊天身份并采样旧消息，再持久化触发标记并点击一次。消息证据要求当前会话的己方新消息、`type=5`、`aweType=507` 及一致资源，不能以面板图片或所有新图片计数判定成功。
- 原生终态识别 SDK `Succeeded(3)`、服务器回收的 `Received(4)`，以及严格的服务器加载形态：`flightStatus` 为 JS `undefined`（不含 `null`）、`isOffline=false`、`serverStatus=0`、正十进制服务端 ID 和 V2。所有形态仍要求唯一新 clientId、V2 超过点击前水位及持续稳定；不把旧气泡、缺字段或对端已读状态当作本次成功。
- 准备完成的快照直接作为首次基线，点击前完整快照由三次减为两次；服务层不重复执行原生适配器已承担的核验。保留面板准备前后及触发回调后紧邻点击的身份检查，文本路径不变。15 秒上限、1.5/0.75 秒稳定窗口和默认好友间等待不变。
- “验证配置”只准备面板与验证目标，绝不点击表情；找不到、歧义或身份冲突明确拒绝，不退回文本。

## 12. 批次策略

- 全量批次加载全部启用目标，跳过停用目标，按桌面列表顺序串行处理，不按好友数量截断。
- 不并发发送。
- 取消数量上限不改变发送前身份核对、当天防重复及 `unknown` 不自动重发规则。
- 默认在两次实际发送尝试之间按计划范围随机等待 `3–8` 秒；允许配置 `0–120` 秒，`0–0` 关闭。
- 第一位发送前、最后一位发送后、配置验证和当天重复跳过不额外等待；等待期间取消会阻止尚未触发的目标。
- 随机等待只降低连续发送频率，不是平台风控绕过，也不保证账号不会受限。
- 页面状态所需的固定等待用于正确性，不计入好友间随机等待。
- 目标级定位或输入失败：记录后继续。
- 登录、安全验证、页面整体不可用：停止剩余目标。
- 取消请求只影响尚未触发发送的目标。

## 13. 日志与隐私

- 目标使用真实名称加本地 ID 的唯一标签，同名好友不会合并进度行。
- 服务、worker 和界面错误统一通过 `format_error` 保留异常原文、原因链、备注和调用栈。
- 查找失败区分无候选、身份不匹配、行不存在或不可见；附搜索框实际值、过滤计数、候选身份和匹配数量。
- `safe_url` 和摘要函数继续用于身份规范化，不作为事件文本脱敏器；日志界面提供可复制的完整 PlainText 详情。
- Windows 通知只显示统计信息。
- 不主动转储聊天正文或登录凭据；原始异常中的本机路径、名称和调用参数由用户自行保管。
- 默认不启用页面截图或 Trace。

## 14. Windows 计划任务

桌面程序生成 Task Scheduler XML，关键设置：

- 每日 CalendarTrigger。
- 本地开始时间。
- `LogonType=InteractiveToken`。
- `RunLevel=LeastPrivilege`。
- `WakeToRun=true`。
- `StartWhenAvailable=false`，防止系统恢复后擅自补发。
- `MultipleInstancesPolicy=IgnoreNew`。
- `RunOnlyIfNetworkAvailable=true`。
- 源码模式使用绝对 Python、模块参数和项目工作目录。
- 冻结程序使用固定程序目录中的 `SparkKeeper.exe --scheduled` 和该目录作为工作目录；首次从便携目录迁入安装目录后重新保存计划以更新绝对路径。

### 14.1 Windows 分发与便携更新

- PyInstaller `onedir` 打包 Python、PySide6/Qt Widgets、Playwright 驱动和 Windows-Toasts；Qt 使用可替换的动态库，发行包附开源许可及来源说明，不捆绑 Tk。
- 仅捆绑实际使用的 Chromium、Headless Shell、FFmpeg 和 Playwright 辅助组件。
- 冻结程序把 `PLAYWRIGHT_BROWSERS_PATH` 指向 EXE 同级 `browsers`，缺失时以固定安全错误拒绝启动网页任务。
- `SparkKeeper.exe` 同时承载桌面入口和隐藏的 `--scheduled` worker 入口。
- 分发构建前扫描并拒绝数据库、登录 DPAPI 密文、任务 XML 及 `local-data`。
- 发行包包含项目及第三方许可、使用说明；安装 EXE 与 ZIP 分别提供 SHA-256 校验文件，校验不等同于发布者数字签名。
- 程序目录包含 `release-manifest.json`：产品、版本及全部发行文件的 SHA-256（不含清单自身），用于更新包完整性核验，不作为数字签名或运行时库替换限制。
- 更新器拒绝危险/重叠/重解析路径、损坏或额外文件、降级、运行中程序及启用的计划；先同卷暂存，再将旧目录保留为备份并换入新版。失败恢复目录，不自动恢复数据库。旧开发包无清单时需显式接受迁入，完整旧目录保留。
- 用户启用计划后不得移动解压目录；移动前需先停用计划，移动后重新启用。

### 14.2 用户级安装器

- 使用 Inno Setup 包装同一 PyInstaller 程序目录，默认安装到 `%LOCALAPPDATA%\Programs\SparkKeeper`；固定 AppId 关联已有安装，不提权。已有安装必须沿用注册路径升级，拒绝另选目录或 `/DIR` 改道，并核对注册 DisplayVersion 防止降级。换位置需先标准卸载保留数据再安装，随后重新保存计划。
- 构建顺序为 `tools/build_portable.py` 后 `tools/build_installer.py`，需要 Inno Setup 6.5+（6.x）编译器，可用 `--iscc` 指定。简体中文语言文件固定于 `tools/installer/ChineseSimplified.isl`，保留上游翻译者说明及许可，不在构建时联网下载。默认产物在 `outputs/release`，安装 EXE 附 `.exe.sha256`；本地 `.exe.build.json` 绑定 EXE、安装脚本/辅助脚本及 payload 清单，发布预检使用但不上传。构建工具不执行安装器。
- 安装、升级和卸载前要求用户停用并保存每日计划、等待发送/扫描/登录及独立 worker 结束、正常退出；占用时拒绝继续，不强杀进程，不自动补跑。
- 升级与卸载要求原目录发行清单和所列文件 SHA-256 正确；未知额外文件拒绝处理，只排除已知安装器自身文件。无清单旧便携目录不得原地覆盖，改为新默认目录安装并保留原便携目录；失败保留日志，不盲目清理未知文件。
- 安装目录与 `%LOCALAPPDATA%\SparkKeeper` 数据目录分离。首次从便携版迁入不复制或覆盖用户数据，但必须在新目录重新保存计划并更新快捷方式；卸载保留数据目录。
- 不把 ZIP 更新器的旧目录备份机制当作安装器自动回滚保证；数据库迁移沿用程序现有一致性备份与拒绝未知高版本 schema 的保护，不自动恢复旧业务数据。
- 安装包未签名，用户文档说明 SmartScreen 风险及可信来源/校验要求，不要求关闭安全软件或绕过组织策略。
- 安装引擎依照 Inno Setup 自身许可使用；SparkKeeper 自有代码的非商业许可不重新许可第三方组件，也不因安装引擎允许商业使用而放宽项目许可。

## 15. 错过计划

桌面程序启动时：

1. 恢复遗留 `sending` 为 `unknown`。
2. 计算最近应执行的计划日期时间。
3. 查询该计划日期是否存在 scheduled 批次。
4. 若不存在，创建一条 `missed_schedule` 待处理事项。
5. 多日未运行时只把最近一次保留为可补发，旧日期记录为过期，避免一次性补发多天。
6. 用户选择补发时，以当前本地日期执行 `missed` 批次并重新检查当天去重。

## 16. 测试策略

自动化测试覆盖：

- DPAPI 往返和密文不含明文。
- 数据库迁移、发送预留、成功、失败、未知和崩溃恢复。
- 当天去重和手动覆盖。
- 运行互斥。
- 好友候选去重和身份匹配纯逻辑。
- 发送终态机的 pending→success、pending→failed、超时→unknown。
- 页面状态错误分类。
- 批次遇到目标错误继续，遇到认证错误停止。
- 计划 XML 的用户会话、唤醒、绝对路径和禁止恢复即补发设置。
- 错过计划检测。
- 完整事件、异常原因链及诊断备注保留；Windows 通知仍只包含统计。

真实验收必须使用测试账号完成扫码、验证配置和一条明确测试消息。自动化测试不能替代真实网页验收。
