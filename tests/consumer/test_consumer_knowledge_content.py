"""用独立构造的 OOXML/Markdown/CSV 驱动真实内容编译器，不包含私有业务原件。"""

import csv
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("content_builder", ROOT / "tools/build_consumer_knowledge.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def workbook(path, sheets):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    book = ET.Element("workbook", xmlns=ns)
    tab = ET.SubElement(book, "sheets")
    relationships = ET.Element(
        "Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships"
    )
    with zipfile.ZipFile(path, "w") as z:
        for n, (name, data) in enumerate(sheets.items(), 1):
            ET.SubElement(tab, "sheet", {"name": name, "sheetId": str(n), f"{{{rel}}}id": f"r{n}"})
            ET.SubElement(relationships, "Relationship", Id=f"r{n}", Target=f"worksheets/sheet{n}.xml")
            sheet = ET.Element("worksheet", xmlns=ns)
            rows = ET.SubElement(sheet, "sheetData")
            for i, values in enumerate([list(data[0]), *[list(row.values()) for row in data]], 1):
                row = ET.SubElement(rows, "row", r=str(i))
                for j, value in enumerate(values):
                    cell = ET.SubElement(row, "c", r=f"{chr(65 + j)}{i}", t="inlineStr")
                    ET.SubElement(ET.SubElement(cell, "is"), "t").text = value
            z.writestr(f"xl/worksheets/sheet{n}.xml", ET.tostring(sheet))
        z.writestr("xl/workbook.xml", ET.tostring(book))
        z.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relationships))


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    row = dict.fromkeys(sorted(builder.REQUIRED), "运营配置")
    row.update(
        {
            "KB_ID": "KB-C-001",
            "用户常见问法/同义词": "如何开始？;How to start?",
            "标准答案（中文）": "按设备提示操作。",
            "标准答案（English）": "Follow the charger instructions.",
            "发布状态": "草稿待业务确认",
            "关联媒体素材ID": "IMG-001",
        }
    )
    sheets = {
        "C端FAQ知识库": [row],
        "多媒体素材清单": [{"素材ID": "IMG-001", "关联FAQ_ID": "KB-C-001", "实际文件URL/存储地址": ""}],
        "意图与API映射": [
            {
                "意图ID": "INT-01",
                "关联FAQ_ID": "KB-C-001~001",
                "建议工具/API能力（逻辑名）": "session_status;stop_session",
            }
        ],
        "转人工与安全规则": [{"规则ID": "ESC-001", "动作": "提供渠道"}],
        "上线配置清单": [{"配置ID": "CFG-01", "配置": "待确认"}],
        "客服Prompt": [{"配置项": "system", "内容": "系统规则"}],
    }
    workbook(source / "content.xlsx", sheets)
    with (source / "MEDIA_ASSET_MANIFEST.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(sheets["多媒体素材清单"][0]))
        writer.writeheader()
        writer.writerows(sheets["多媒体素材清单"])
    (source / "00_System_Prompt.md").write_text("# Prompt\n系统规则")
    (source / "knowledge_markdown").mkdir()
    (source / "knowledge_markdown/01.md").write_text(
        "## KB-C-001 | 开始\n\n**标准答复（中文）**\n按设备提示操作。\n\n"
        "**Standard answer (English)**\nFollow the charger instructions.\n\n"
        "**关联媒体素材 / Media assets:** IMG-001\n"
    )
    return source, sheets


def test_real_bundle_keeps_draft_provenance_and_never_promotes_writes(package, tmp_path):
    source, _ = package
    output = tmp_path / "out"
    summary = builder.write_bundle(source, output)
    data = json.loads((output / "content.json").read_text())
    assert summary == {
        "faqs": 1,
        "categories": 1,
        "query_variants": 2,
        "media_planned": 1,
        "media_with_address": 0,
        "media_conflicts": 0,
        "publishable": False,
    }
    assert data["entries"][0]["source_status"] == "草稿待业务确认"
    assert data["workflow_scope"] == {
        "write_tools_disabled": ["stop_session"],
        "read_candidates_unverified": ["session_status"],
    }
    text = (output / "zh/KB-C-001.md").read_text()
    assert "按设备提示操作。" in text
    assert not any(word in text for word in ["IMG-001", "stop_session", "运营配置"])
    assert (output / "content.json").stat().st_mode & 0o777 == 0o600
    before = (output / "content.json").read_bytes()
    with pytest.raises(ValueError, match="new directory"):
        builder.write_bundle(source, output)
    assert (output / "content.json").read_bytes() == before


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("answer", "answer differs"),
        ("unknown_media", "media references"),
        ("duplicate", "duplicate KB_ID"),
        ("csv", "media CSV"),
        ("missing_sheet", "no sheets"),
        ("blank", "required FAQ"),
    ],
)
def test_bad_source_refuses_before_creating_output(package, tmp_path, mutation, error):
    source, sheets = package
    md = source / "knowledge_markdown/01.md"
    if mutation == "answer":
        md.write_text(md.read_text().replace("按设备提示操作。", "无条件直接插入。"))
    elif mutation == "unknown_media":
        md.write_text(md.read_text().replace("IMG-001", "IMG-999"))
    elif mutation == "duplicate":
        sheets["C端FAQ知识库"] *= 2
    elif mutation == "csv":
        (source / "MEDIA_ASSET_MANIFEST.csv").write_text("invalid\n")
    elif mutation == "missing_sheet":
        del sheets["上线配置清单"]
    elif mutation == "blank":
        sheets["C端FAQ知识库"][0]["转人工条件"] = ""
    workbook(source / "content.xlsx", sheets)
    with pytest.raises(ValueError, match=error):
        builder.write_bundle(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_known_but_inconsistent_media_stays_an_explicit_conflict(package):
    source, sheets = package
    sheets["多媒体素材清单"][0]["关联FAQ_ID"] = ""
    workbook(source / "content.xlsx", sheets)
    with (source / "MEDIA_ASSET_MANIFEST.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(sheets["多媒体素材清单"][0]))
        writer.writeheader()
        writer.writerows(sheets["多媒体素材清单"])
    result = builder.compile_package(source)
    assert result["media_conflicts"] == [
        {"faq_id": "KB-C-001", "asset_id": "IMG-001", "faq_declares": True, "media_declares": False}
    ]
    assert not result["summary"]["publishable"]
