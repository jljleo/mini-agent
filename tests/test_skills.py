"""skills 机制回归测试：frontmatter 解析容错 / 扫描 / 索引格式 / ChatSession 注入。"""

import led_review.config as config
import led_review.kernel.agent as agent
from led_review.skills import _parse_frontmatter, format_skills_index, scan_skills

STANDARD = """---
name: commit-style
description: 提交信息规范：type(scope): 主题
---

## 约定

- 主题不超过 50 字
"""


def make_skill(root, dirname, text=STANDARD):
    d = root / "skills" / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


class TestFrontmatter:
    def test_standard(self):
        fields, body = _parse_frontmatter(STANDARD)
        assert fields == {"name": "commit-style", "description": "提交信息规范：type(scope): 主题"}
        assert body.startswith("## 约定")

    def test_no_frontmatter(self):
        fields, body = _parse_frontmatter("# 直接正文\n内容")
        assert fields == {} and body.startswith("# 直接正文")

    def test_unclosed_fence_treated_as_body(self):
        fields, body = _parse_frontmatter("---\nname: x\n没有闭合")
        assert fields == {} and "name: x" in body

    def test_malformed_lines_ignored(self):
        fields, _ = _parse_frontmatter("---\nname: x\n这不是键值行\n\n---\n正文")
        assert fields == {"name": "x"}


class TestScan:
    def test_scan_sorted_with_fallbacks(self, tmp_path):
        make_skill(tmp_path, "beta")
        # 无 frontmatter：name 回落目录名，description 回落正文首行
        make_skill(tmp_path, "alpha", "# 部署流程\n先跑测试\n")
        skills = scan_skills(str(tmp_path))
        assert [s.name for s in skills] == ["alpha", "commit-style"]  # 按目录名排序
        alpha = next(s for s in skills if s.path.startswith("skills/alpha"))
        assert alpha.name == "alpha" and alpha.description == "部署流程"
        assert alpha.chars > 0

    def test_missing_dir_returns_empty(self, tmp_path):
        assert scan_skills(str(tmp_path)) == []

    def test_dir_without_skill_file_skipped(self, tmp_path):
        (tmp_path / "skills" / "empty").mkdir(parents=True)
        make_skill(tmp_path, "ok")
        assert [s.name for s in scan_skills(str(tmp_path))] == ["commit-style"]

    def test_garbled_skill_file_skipped(self, tmp_path):
        """非 UTF-8 的 SKILL.md 抛 UnicodeDecodeError（ValueError 子类，不是 OSError），
        必须纳入 per-skill 容错——dogfood review 首跑抓到的真 bug。"""
        bad = tmp_path / "skills" / "bad"
        bad.mkdir(parents=True)
        (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00\x01")
        make_skill(tmp_path, "ok")
        assert [s.name for s in scan_skills(str(tmp_path))] == ["commit-style"]


class TestFormat:
    def test_none_when_empty(self):
        assert format_skills_index([]) is None

    def test_index_contains_path_and_cost(self, tmp_path):
        make_skill(tmp_path, "commit-style")
        text = format_skills_index(scan_skills(str(tmp_path)))
        assert "commit-style" in text and "提交信息规范" in text
        assert "skills/commit-style/SKILL.md" in text and "字符" in text


class TestInjection:
    def test_injected_as_system_message(self, tmp_path, monkeypatch):
        make_skill(tmp_path, "commit-style")
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
        session = agent.ChatSession(set_provider=False)
        texts = [m.get("content", "") for m in session.messages if m.get("role") == "system"]
        assert any("commit-style" in t and "read_file" in t for t in texts), \
            "skills 索引应注入为 system 消息并指引 read_file 加载"

    def test_missing_dir_not_injected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
        session = agent.ChatSession(set_provider=False)
        texts = [m.get("content", "") for m in session.messages if m.get("role") == "system"]
        assert not any("skills" in t for t in texts), "无 skills 目录应零注入"
