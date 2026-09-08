from __future__ import annotations

from dataclasses import dataclass

from ..models import ErrorCode


@dataclass(eq=False)
class AutomationError(RuntimeError):
    code: ErrorCode
    safe_message: str
    fatal: bool = False
    send_triggered: bool = False

    def __str__(self) -> str:
        return self.safe_message


class AuthenticationRequired(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.AUTHENTICATION_REQUIRED, "登录状态已失效", fatal=True)


class HumanVerificationRequired(AutomationError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.HUMAN_VERIFICATION_REQUIRED,
            "抖音要求用户本人完成验证",
            fatal=True,
        )


class PageNotReady(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.PAGE_NOT_READY, "私信页面未能及时就绪", fatal=True)


class PageStructureChanged(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.PAGE_STRUCTURE_CHANGED, "私信页面结构可能已经变化", fatal=True)


class TargetNotFound(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.TARGET_NOT_FOUND, "没有找到已确认的好友")


class TargetAmbiguous(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.TARGET_AMBIGUOUS, "搜索结果无法唯一确认好友")


class TargetIdentityMismatch(AutomationError):
    def __init__(self, safe_message: str = "当前聊天对象与已确认好友不一致") -> None:
        super().__init__(ErrorCode.TARGET_IDENTITY_MISMATCH, safe_message)


class ComposerUnavailable(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.COMPOSER_UNAVAILABLE, "聊天输入框不可用")


class SendFailed(AutomationError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.SEND_FAILED, "页面明确显示消息发送失败", send_triggered=True)


class SendUnknown(AutomationError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.SEND_UNKNOWN,
            "已经触发发送，但无法确认最终结果",
            send_triggered=True,
        )
