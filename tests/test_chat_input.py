"""genesis_agent.chat_input — a pasted or fenced multi-line task is ONE message."""
from genesis_agent.chat_input import read_message


def _feed(lines):
    it = iter(lines)

    def nxt():
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return nxt


def test_a_typed_line_is_one_message():
    more = _feed(["изход"])
    assert read_message(lambda: "здрасти", more, pending=lambda: False) == "здрасти"
    assert more() == "изход"  # the next line is left for the next message


def test_a_paste_is_one_message():
    pasted = ["```python", "def f(n):", "    return n", "```"]
    left = [len(pasted)]

    def pending():
        left[0] -= 1
        return left[0] >= 0
    msg = read_message(lambda: "Напиши функцията:", _feed(pasted), pending=pending)
    assert msg == "Напиши функцията:\n```python\ndef f(n):\n    return n\n```"


def test_a_block_is_one_message_and_keeps_blank_lines():
    more = _feed(["Задача:", "", "  отстъп", '"""', "изход"])
    assert read_message(lambda: '"""', more, pending=lambda: False) == "Задача:\n\n  отстъп"
    assert more() == "изход"


def test_eof_inside_a_block_ends_the_block_not_the_chat():
    assert read_message(lambda: '"""', _feed(["едно", "две"]), pending=lambda: False) == "едно\nдве"
