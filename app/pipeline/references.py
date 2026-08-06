"""正文末尾那份参考文献清单：怎么认出它，怎么把它摘下来。

清单是流水线自己拼的，不是谁写的句子，而正文里的每一个 [n] 都指着它。所以
凡是要把整篇正文交给模型的地方，都得先摘下来、事后原样接回去。模型不会恶意
删它，只是被要求「按模板的结尾结构重写」时顺手就把它换掉了——满篇 [n] 于是
成了断头指针，不报错，也没人会立刻发现。

清单永远在正文最后，`split` 之后两段拼起来等于原文。
"""

from __future__ import annotations

import re

HEADING = "## 参考文献"

# 合成器写的是「正文 \n\n---\n\n## 参考文献」。分隔线属于清单的一部分，一起
# 摘走才能原样接回去，不然每重写一次正文末尾就多一条横线。
_TAIL = re.compile(
    r"\n+(?:-{3,}[ \t]*\n+)?[ \t]{0,3}#{1,6}[ \t]*"
    r"(?:参考文献|参考资料|References)[ \t]*(?:\n|$)",
    re.IGNORECASE)


def split(body_md: str) -> tuple[str, str]:
    """把正文拆成「人读的部分」和「机器拼的参考文献」。

    没有清单时第二项是空串。
    """
    text = body_md or ""
    last = None
    for match in _TAIL.finditer(text):
        last = match                    # 清单在最后，同名小节以最后一处为准
    if last is None:
        return text, ""
    return text[:last.start()], text[last.start():]


def restore(body_md: str, tail: str) -> str:
    """把 :func:`split` 摘下的清单接回重写过的正文。

    模型有时会自己也补一份参考文献。真话只有一份，以摘下来的那份为准。

    ``tail`` 自带前导空行，直接接上就还原成原样。少了那个空行，紧跟在正文后
    的 ``---`` 会被 markdown 当成 setext 标题，把最后一段变成大标题。
    """
    if not tail:
        return body_md
    return split(body_md)[0].rstrip() + tail
