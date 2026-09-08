# SparkKeeper · 续火花助手

Windows 本地桌面工具，用于向本人已获授权的抖音好友发送固定文本或原生“续火花”表情。使用网页交互，不提供验证码、风控绕过、群发营销或平台效果保证。

源码仓库：https://github.com/Charlie-chulong/SparkKeeper

发行下载：https://github.com/Charlie-chulong/SparkKeeper/releases

## 使用

1. 从 GitHub Release 下载 Windows ZIP，完整解压，不要只复制 EXE。
2. 首次使用，将包内 `SparkKeeper` 文件夹放到当前用户可写的固定目录，例如 `D:\软件\SparkKeeper`，运行其中的 `SparkKeeper.exe`。
3. 本人扫码登录、确认好友与发送内容，先执行“验证配置（不发送）”。
4. 手动发送前核对整批预览；每日计划需明确启用并保存。

文本与原生表情互斥。原生“续火花”不是小表情“[续火花吧]”，表情预览不代表已发送。发送结果不确定时保留当天防重复保护，不自动重试；请人工核对聊天，不要直接覆盖重发。

## 单实例

从 3.0.0 起，同一 Windows 用户与登录会话只保留一个主窗口。再次运行程序会请求唤醒已有窗口，不重复初始化业务数据。每日计划 worker 与 GUI 单实例机制分离。

第一次从旧开发版升级时，请先退出所有旧版窗口：旧版没有单实例通信机制，不能由新版可靠接管。`--smoke` 是独立诊断入口，不用于检查正常单实例行为。

## 离线更新

1. 下载并完整解压新版本 ZIP 到另外的临时目录；不要直接合并覆盖旧文件夹。
2. 在旧版停用每日计划，等待发送、扫描、登录等操作结束，正常退出程序。
3. 双击新包根目录的 `更新.cmd`，按提示选择旧程序目录。
4. 更新入口检查包内容、版本及占用，准备同卷临时副本后完整替换。成功后核对新版界面和配置，再重新启用计划。

程序目录必须固定；原先使用带版本目录的用户可先迁到固定目录，并在新路径重新保存每日计划。更新不会替用户取消发送中的进程、绕过确认或触发补跑。

更新器拒绝损坏包、未知目标目录、降级及危险路径。旧开发包没有文件清单时需要明确接受旧版迁入；无法逐个证明旧依赖来源，旧目录会完整保留为备份。旧目录中额外的用户文件应先自行移出，不能当作应用依赖静默删除。

整目录替换失败时尝试恢复旧程序。数据库迁移前另做一致性备份；新版已产生发送记录后，不能自动用旧数据库覆盖，否则可能丢失防重复依据。旧程序不兼容新数据库时应使用修复版，不要强行降级。

`更新.cmd` 调用 Windows PowerShell，只对本次进程设置执行策略，不提权、不联网、不永久修改系统策略。组织策略禁用脚本时不会绕过组织限制。

## 数据与隐私

正式 EXE 的运行数据位于 `%LOCALAPPDATA%\SparkKeeper`，与程序目录分离。好友、完整消息文本、发送历史及原始错误事件会保存在本机；登录态由当前 Windows 用户的 DPAPI 加密。关闭“不确定结果”提醒只表示已知晓，不改变发送结果或防重复记录。

源码运行的默认数据目录为 `work/local-data`。`SPARK_KEEPER_ROOT` 属于开发/测试覆盖配置；设置了该变量时离线更新器拒绝盲目更新。

不要将数据库、登录态、任务 XML、诊断目录、私人截图或 Token 上传到仓库或随发行包传播。本仓库忽略整个 `work/` 和 `outputs/`。备份同样包含敏感数据，应仅留本机。

## 开发与构建

要求 Windows x64、Python 3.12 或更高版本，以及 Git；人工 GitHub 发布还需要 GitHub CLI 和本人授权登录。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check src tests tools
.venv\Scripts\python.exe tools\build_portable.py
```

版本唯一来源是 `src/spark_keeper/__init__.py` 的 `__version__`。正式版本使用 `3.0.0`；候选版本在 Python 中使用 `3.0.1rc1`，对外标签使用 `v3.0.1-rc.1`。构建与发布工具校验版本，不通过目录名推断运行版本。

发行 ZIP 包含固定的 `SparkKeeper/` 程序目录，以及根目录更新入口。程序目录中的 `release-manifest.json` 用于离线检查产品、版本及文件 SHA-256；Release 的 ZIP 校验文件用于检测下载损坏。它们都不是发布者数字签名，请仅从可信 Release 获取程序。

## 人工发布

1. 修改单一版本号并补充 `CHANGELOG.md`。
2. 完成测试、构建、解压运行及更新验证，确认没有运行数据被跟踪。
3. 提交源码，创建与版本一致的 `v3.0.0` 标签，并推送分支和标签。
4. 运行 `tools/publish_release.py --help` 查看本版命令；默认仅预检，明确使用 `--publish` 才创建 GitHub Release 草稿和上传资产。
5. 在 GitHub 检查草稿中的版本、发布说明、ZIP、校验文件和 Qt 对应源码，人工发布后再分发下载链接。

发布目标为 `Charlie-chulong/SparkKeeper`。软件不保存或携带 GitHub Token；发布凭据仅由开发者自己的 GitHub CLI 管理。当前不提供联网自动更新，也不配置每次提交自动发布。

## 第三方组件

见 `THIRD_PARTY_NOTICES.txt` 和发行包 `licenses/`。Qt/PySide6 动态库按 LGPL v3 分发，对应未修改源码随 Release 提供；允许用兼容修改版动态库替换并调试。发布清单用于更新包完整性检查，不限制运行时替换 LGPL 组件。

抖音原生表情预览资源属于其权利人，仅用于展示所选原生资源，不作为本软件图标。公开仓库不意味着第三方素材授权发生改变；本项目尚未另行授予通用开源许可证。
