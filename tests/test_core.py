"""核心纯函数回归测试（不依赖 Flask / 模型加载，导入 server_gguf 即可）。

覆盖：build_translate_prompt / looks_like_echo / parse_numbered_output / is_same_language
—— 这几处是最易藏 bug、又无客户端直接调用的逻辑。
"""
import os
import sys

import pytest

# 让测试能在仓库根目录直接 import server_gguf
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server_gguf as s  # noqa: E402


# --------------------------------------------------------------------------
# build_translate_prompt
# --------------------------------------------------------------------------
def test_short_text_uses_official_template():
    prompt = s.build_translate_prompt("Hello world", "zh")
    assert "把下面的文本翻译成中文，不要额外解释。" in prompt
    assert "Hello world" in prompt
    # 短文本不带规则版长指令
    assert "You are a professional" not in prompt


def test_long_text_uses_full_rules_prompt():
    long_text = "This is a fairly long piece of text. " * 15  # > 300 字符
    prompt = s.build_translate_prompt(long_text, "zh")
    assert "You are a professional translation engine" in prompt
    assert "TEXT TO TRANSLATE:" in prompt
    assert long_text in prompt


def test_glossary_injected_into_prompt():
    text = "We discuss angst and EBITDA in the report."  # > 300? no, but glossary forces full
    # glossary 强制走长模板，因此用带 glossary 的短文本即可验证注入
    prompt = s.build_translate_prompt("angst", "zh", glossary={"angst": "焦虑"})
    assert "GLOSSARY" in prompt
    assert "- angst = 焦虑" in prompt


def test_glossary_capped_at_80_entries():
    glossary = {f"k{i}": f"v{i}" for i in range(200)}
    prompt = s.build_translate_prompt("x", "zh", glossary=glossary)
    injected = [ln for ln in prompt.splitlines() if ln.startswith("- ") and " = " in ln]
    assert len(injected) == 80


def test_context_appended():
    prompt = s.build_translate_prompt("x" * 301, "en", context="previous line")
    assert "CONTEXT" in prompt
    assert "previous line" in prompt


# --------------------------------------------------------------------------
# looks_like_echo
# --------------------------------------------------------------------------
def test_looks_like_echo_detects_markers():
    assert s.looks_like_echo("You are a professional translation engine.") is True
    assert s.looks_like_echo("TEXT TO TRANSLATE: hello") is True
    assert s.looks_like_echo("RULES: keep proper nouns") is True


def test_looks_like_echo_empty_is_true():
    assert s.looks_like_echo("") is True
    assert s.looks_like_echo("   ") is True


def test_looks_like_echo_normal_translation_false():
    assert s.looks_like_echo("这是一段正常的中文译文。", source="This is a normal sentence.") is False


def test_looks_like_echo_too_long_vs_source():
    src = "short"
    long_output = "x" * 500
    assert s.looks_like_echo(long_output, source=src) is True


# --------------------------------------------------------------------------
# parse_numbered_output
# --------------------------------------------------------------------------
def test_parse_numbered_basic():
    raw = "1. 你好\n2. 世界"
    out = s.parse_numbered_output(raw, 2)
    assert out == {1: "你好", 2: "世界"}


def test_parse_numbered_with_multiline_body():
    raw = "1. 第一句\n第二行\n2. 第二句"
    out = s.parse_numbered_output(raw, 2)
    assert out[1] == "第一句 第二行"
    assert out[2] == "第二句"


def test_parse_numbered_missing_entry_is_none():
    raw = "1. 你好"
    out = s.parse_numbered_output(raw, 3)
    assert out[1] == "你好"
    assert out[2] is None
    assert out[3] is None


def test_parse_numbered_variants():
    raw = "1、苹果\n(2) 香蕉\n3：橙子"
    out = s.parse_numbered_output(raw, 3)
    assert out[1] == "苹果"
    assert out[2] == "香蕉"
    assert out[3] == "橙子"


def test_parse_numbered_out_of_range_ignored():
    # 序号超过 count 不会生成新的顶层条目（9 不进入 result 的键集合）
    raw = "1. 你好\n9. 越界"
    out = s.parse_numbered_output(raw, 2)
    assert set(out.keys()) == {1, 2}
    assert 9 not in out
    # 越界编号行被当作当前条目正文续接（既有行为，回归锁定）
    assert "9. 越界" in out[1]


# --------------------------------------------------------------------------
# is_same_language
# --------------------------------------------------------------------------
def test_is_same_language_chinese():
    assert s.is_same_language("这是一段中文文本", "zh") is True
    assert s.is_same_language("This is English text", "zh") is False


def test_is_same_language_english():
    assert s.is_same_language("This is English text", "en") is True
    assert s.is_same_language("这是中文", "en") is False


def test_is_same_language_empty_false():
    assert s.is_same_language("", "zh") is False
    # 纯数字/标点无字母汉字，无法判定 -> False
    assert s.is_same_language("123.45!", "zh") is False


# --------------------------------------------------------------------------
# 安全辅助函数
# --------------------------------------------------------------------------
def test_safe_upload_name_strips_traversal():
    assert ".." not in s._safe_upload_name("../../../etc/passwd")
    assert "/" not in s._safe_upload_name("a/b/c.pdf")
    assert s._safe_upload_name("../../../etc/passwd").endswith("passwd")


def test_clamp_max_tokens():
    assert s._clamp_max_tokens(1) == 1
    assert s._clamp_max_tokens(999999) == s.MAX_CHAT_TOKENS
    assert s._clamp_max_tokens("not-int") == 2048
    assert s._clamp_max_tokens(None) == 2048
