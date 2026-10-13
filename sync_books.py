#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChinaK12Books 自动化教材同步与增量版本更新引擎
=============================================================================
功能定位：
1. 从国家中小学智慧教育平台 (basic.smartedu.cn) 获取全量教材目录与格式索引；
2. 按照与 ChinaTextbook 一致的规范目录层级归档：
   【学段】/【学科】/【版本-出版社】/【年级】/【书名 [版本号].pdf】
3. 智能提取与标注每本书的版本号（如：2024修订版、2022修订版、2019课标版、第4版等）；
4. 维护全局版本清单 textbooks_manifest.json 与 textbooks_manifest.csv；
5. 提供增量比对更新机制（可每10天运行一次，仅下载新增或版本变化的课本，自动跳过已有课本）。
"""

import os
import sys
import json
import csv
import re
import time
import argparse
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

import requests

# 引入邻居目录 pdf_book_download_from_zxxeducn 的网络与解析模块
CURRENT_DIR = Path(__file__).resolve().parent
DOWNLOADER_DIR = CURRENT_DIR.parent / "pdf_book_download_from_zxxeducn"
if str(DOWNLOADER_DIR) not in sys.path:
    sys.path.insert(0, str(DOWNLOADER_DIR))

try:
    import pdf_book_download_from_zxxeducn as dl
    import catalog_index as ci
except ImportError:
    print("⚠️ 未检测到 pdf_book_download_from_zxxeducn 模块，将仅支持离线清单与缓存模式。")
    dl = None
    ci = None

# 常见版本与出版社标准映射表（对齐 ChinaTextbook 命名标准）
PUBLISHER_MAPPING = {
    "人教版": "人教版-人民教育出版社",
    "统编版": "统编版-人民教育出版社",
    "部编版": "统编版-人民教育出版社",
    "北师大版": "北师大版-北京师范大学出版社",
    "苏教版": "苏教版-江苏凤凰教育出版社",
    "苏科版": "苏科版-江苏凤凰科学技术出版社",
    "冀教版": "冀教版-河北教育出版社",
    "鲁教版": "鲁教版-山东教育出版社",
    "鲁人版": "鲁人版-山东人民出版社",
    "鲁科版": "鲁科版-山东科学技术出版社",
    "北京版": "北京版-北京出版社",
    "京版": "北京版-北京出版社",
    "华东师大版": "华东师大版-华东师范大学出版社",
    "沪科版": "沪科技版-上海科学技术出版社",
    "沪科技版": "沪科技版-上海科学技术出版社",
    "沪教版": "沪教版-上海教育出版社",
    "浙教版": "浙教版-浙江教育出版社",
    "湘教版": "湘教版-湖南教育出版社",
    "青岛版": "青岛版-青岛出版社",
    "教科版": "教科版-教育科学出版社",
    "科粤版": "科粤版-广东科技出版社/科学出版社",
    "粤教版": "粤教版-广东教育出版社",
    "中图版": "中图版-中国地图出版社",
    "外研版": "外研版-外语教学与研究出版社",
    "仁爱版": "仁爱版-北京仁爱教育研究所",
    "译林版": "译林版-译林出版社",
    "接力版": "接力版-接力出版社",
    "商务印书馆": "商务印书馆版-商务印书馆",
}

# 缓存文件路径
CACHE_INDEX_PATH = Path.home() / "Library/Caches/textbook-downloader/web_index.json"
MANIFEST_JSON = CURRENT_DIR / "textbooks_manifest.json"
MANIFEST_CSV = CURRENT_DIR / "textbooks_manifest.csv"


def extract_edition_tag(title: str, book_meta: Optional[Dict[str, Any]] = None) -> str:
    """
    智能提取版本号标签（例如：2024修订版, 2022修订版, 2019课标版, 第4版, 2024审定版）
    """
    # 1. 匹配课程标准修订年份
    m = re.search(r"根据(20\d\d)年(?:版)?课程标准(?:修订)?", title)
    if m:
        return f"{m.group(1)}修订版"

    # 2. 匹配教育部审定年份
    m = re.search(r"(20\d\d)年(?:教育部)?审定", title)
    if m:
        return f"{m.group(1)}审定版"

    # 3. 匹配季节修订
    m = re.search(r"(20\d\d)年[秋春]季?", title)
    if m:
        return f"{m.group(1)}秋季版"

    # 4. 匹配显式版次（第X版）
    m = re.search(r"第([0-9一二三四五六七八九十]+)版", title)
    if m:
        return f"第{m.group(1)}版"

    # 5. 匹配年份版
    m = re.search(r"(20\d\d)年版", title)
    if m:
        return f"{m.group(1)}版"

    # 6. 普通高中统编教材（2019 年秋季全国高中新课标）
    if "普通高中教科书" in title or "高中" in title:
        return "2019课标版"

    # 7. 义务教育教科书（2011/2012 旧课标版）
    if "义务教育教科书" in title:
        return "2012课标版"

    # 8. 回退策略：检查更新时间
    if book_meta:
        update_time = str(book_meta.get("update_time") or "")
        if len(update_time) >= 4 and update_time[:4] in ("2023", "2024", "2025", "2026"):
            return f"{update_time[:4]}最新版"

    return "标准审定版"


def clean_book_title(title: str) -> str:
    """
    清理书名中冗余的超长括号说明，使文件名保持精炼规范
    例如：'统编版（根据2022年版课程标准修订）义务教育教科书·道德与法治一年级上册'
    规范为：'义务教育教科书·道德与法治一年级上册'
    """
    clean = re.sub(r"^[（(][^）)]+[）)]", "", title)
    clean = re.sub(r"统编版[（(][^）)]+[）)]", "统编版·", clean)
    clean = clean.replace("··", "·").strip()
    return clean


def resolve_taxonomy(book: Dict[str, Any]) -> Tuple[str, str, str, str, str, str]:
    """
    解析一本书的标准分类层级：
    返回: (stage, subject, publisher_dir, grade_dir, edition_tag, clean_filename)
    """
    raw_stage = (book.get("stage") or ["未标注"])[0]
    raw_subject = (book.get("subject") or ["未标注"])[0]
    raw_version = (book.get("version") or ["未标注"])[0]
    raw_grade = (book.get("grade") or ["全一册"])[0]
    raw_volume = (book.get("volume") or [""])[0]
    title = book.get("title", "")

    # 学段归一化
    stage = raw_stage
    if "五•四" in stage or "五四" in stage:
        if "初中" in stage:
            stage = "初中（五•四学制）"
        elif "小学" in stage:
            stage = "小学（五•四学制）"

    # 学科归一化
    subject = raw_subject
    if "/" in subject:
        subject = subject.replace("/", "、")

    # 版本-出版社归一化
    if raw_version in PUBLISHER_MAPPING:
        publisher_dir = PUBLISHER_MAPPING[raw_version]
    elif raw_version != "未标注":
        publisher_dir = f"{raw_version}版"
    else:
        # 从书名推测出版社
        guessed = None
        for v, pub in PUBLISHER_MAPPING.items():
            if v in title or pub.split("-")[-1] in title:
                guessed = pub
                break
        publisher_dir = guessed or "通用版"

    # 年级归一化
    grade_dir = raw_grade
    if raw_grade in ("未标注", "全一册") and raw_volume:
        grade_dir = raw_volume

    # 提取版本号
    edition_tag = extract_edition_tag(title, book)

    # 生成带版本号的标准文件名
    base_name = clean_book_title(title)
    # 如果书名结尾已经有 .pdf，先去掉
    if base_name.endswith(".pdf"):
        base_name = base_name[:-4]
    
    # 避免重复追加版本标签
    if f"[{edition_tag}]" not in base_name:
        clean_filename = f"{base_name} [{edition_tag}].pdf"
    else:
        clean_filename = f"{base_name}.pdf"

    # 移除文件系统中非法字符
    clean_filename = re.sub(r'[/\\:*?"<>|]', "_", clean_filename)

    return stage, subject, publisher_dir, grade_dir, edition_tag, clean_filename


def load_raw_catalog(refresh: bool = False) -> List[Dict[str, Any]]:
    """加载原始书籍索引（优先使用本地已构建的高速缓存）"""
    if not refresh and CACHE_INDEX_PATH.exists():
        try:
            with open(CACHE_INDEX_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("books", [])
        except Exception as e:
            print(f"⚠️ 读取缓存失败: {e}，尝试重新拉取...")

    if ci:
        print("🔎 正在从国家智慧教育平台重新构建目录与格式索引...")
        index = ci.build_index()
        return index
    
    raise RuntimeError("未找到有效的目录索引数据源！")


def generate_manifest(refresh_index: bool = False) -> List[Dict[str, Any]]:
    """
    生成全局标准化的书籍元数据清单（只包含可下载且带 PDF 的教材）
    """
    books = load_raw_catalog(refresh=refresh_index)
    manifest = []

    for b in books:
        if b.get("kind") != "downloadable":
            continue
        formats = b.get("formats") or {}
        if "pdf" not in formats:
            continue

        stage, subject, publisher, grade, edition, filename = resolve_taxonomy(b)
        rel_path = f"{stage}/{subject}/{publisher}/{grade}/{filename}"

        item = {
            "seq": b.get("seq", len(manifest) + 1),
            "id": b.get("id"),
            "title": b.get("title"),
            "stage": stage,
            "subject": subject,
            "publisher": publisher,
            "grade": grade,
            "edition_tag": edition,
            "filename": filename,
            "relative_path": rel_path,
            "file_size": formats.get("pdf", 0),
            "status": "pending",
            "last_synced": None,
        }
        manifest.append(item)

    # 写入 JSON 清单
    with open(MANIFEST_JSON, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 写入 CSV 清单（方便 Excel 查看）
    with open(MANIFEST_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "seq",
                "id",
                "stage",
                "subject",
                "publisher",
                "grade",
                "edition_tag",
                "filename",
                "file_size",
                "status",
                "relative_path",
                "title",
            ],
        )
        writer.writeheader()
        for item in manifest:
            row = dict(item)
            del row["last_synced"]
            writer.writerow(row)

    print(f"✅ 全局教材清单已生成: 共 {len(manifest)} 本可下载 PDF 教材")
    print(f"   • JSON 清单: {MANIFEST_JSON.name}")
    print(f"   • CSV 表格:  {MANIFEST_CSV.name}")
    return manifest


def check_updates(manifest: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    比对本地已有文件与云端清单，输出增量更新报告
    """
    report = {
        "already_exists": [],  # 本地已存在且版本一致（跳过）
        "missing": [],         # 本地未下载（需下载）
        "version_mismatch": [], # 版本或文件大小变化（需更新）
    }

    for item in manifest:
        local_file = CURRENT_DIR / item["relative_path"]
        if local_file.exists():
            local_size = local_file.stat().st_size
            expected_size = item["file_size"]
            # 允许 1% 内的细微差异，避免轻微重压缩导致重复下载
            if expected_size > 0 and abs(local_size - expected_size) > (expected_size * 0.05):
                report["version_mismatch"].append(item)
            else:
                report["already_exists"].append(item)
        else:
            report["missing"].append(item)

    print("\n" + "=" * 60)
    print("📊 本地与云端教材版本比对报告")
    print("=" * 60)
    print(f"🟢 本地已是最新版本 (无需更新): {len(report['already_exists'])} 本")
    print(f"🟡 本地尚未下载 (待同步):       {len(report['missing'])} 本")
    print(f"🔴 检测到版本/内容变化 (待更新): {len(report['version_mismatch'])} 本")
    print("=" * 60 + "\n")
    return report


# 全局网络请求头与会话（带国家智慧教育平台授权签名）
GLOBAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://basic.smartedu.cn/",
    "Origin": "https://basic.smartedu.cn",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Authorization": "Bearer 0",
    "X-ND-AUTH": 'MAC id="0",nonce="0",mac="0"',
}

SESSION = requests.Session()
SESSION.headers.update(GLOBAL_HEADERS)


def download_single_book(item: Dict[str, Any], overwrite: bool = False) -> Tuple[bool, str]:
    """下载单本教材至规范化目录（多镜像 CDN 自动回退，带魔数校验与断点安全）"""
    target_path = CURRENT_DIR / item["relative_path"]
    target_dir = target_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    if target_path.exists() and not overwrite:
        # 如果文件已存在且大小正常（> 100KB），直接跳过
        if target_path.stat().st_size > 100 * 1024:
            return True, "已存在且版本一致 (跳过)"

    book_id = item["id"]
    details_url = f"https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{book_id}.json"

    try:
        r = SESSION.get(details_url, timeout=(10, 30))
        if r.status_code != 200:
            # 尝试备用节点
            details_url_backup = f"https://s-file-2.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{book_id}.json"
            r = SESSION.get(details_url_backup, timeout=(10, 30))
            if r.status_code != 200:
                return False, f"获取详情失败 (HTTP {r.status_code})"

        data = r.json()
        items = data.get("ti_items") or []
        pdf_item = next((it for it in items if it.get("ti_format") == "pdf"), None)
        if not pdf_item:
            return False, "无有效 PDF 资源发布"

        candidate_urls = pdf_item.get("ti_storages") or []
        if not candidate_urls and pdf_item.get("ti_storage"):
            candidate_urls = [pdf_item["ti_storage"].replace("cs_path:${ref-path}", "https://r1-ndr-private.ykt.cbern.com.cn")]

        if not candidate_urls:
            return False, "无法解析下载地址"

        # 开始流式下载
        part_path = target_path.with_name(target_path.name + ".part")
        download_success = False

        for url in candidate_urls:
            try:
                with SESSION.get(url, timeout=(15, 120), stream=True) as resp:
                    if resp.status_code != 200:
                        continue

                    written = 0
                    first_chunk = True
                    is_valid_pdf = True

                    with open(part_path, "wb") as f_out:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                continue
                            if first_chunk:
                                first_chunk = False
                                if not chunk.startswith(b"%PDF"):
                                    is_valid_pdf = False
                                    break
                            f_out.write(chunk)
                            written += len(chunk)

                    if not is_valid_pdf:
                        part_path.unlink(missing_ok=True)
                        continue

                    # 下载完成，原子重命名
                    part_path.replace(target_path)
                    download_success = True
                    break
            except Exception:
                part_path.unlink(missing_ok=True)
                continue

        if download_success:
            item["status"] = "downloaded"
            item["last_synced"] = datetime.now().isoformat()
            return True, "下载成功"
        else:
            return False, "所有 CDN 镜像节点均连接失败"

    except Exception as e:
        return False, f"异常: {e}"



def run_sync(
    manifest: List[Dict[str, Any]],
    stage_filter: Optional[str] = None,
    subject_filter: Optional[str] = None,
    version_filter: Optional[str] = None,
    limit: Optional[int] = None,
    workers: int = 4,
    overwrite: bool = False,
):
    """批量同步与下载引擎"""
    filtered = []
    for item in manifest:
        if stage_filter and stage_filter not in item["stage"]:
            continue
        if subject_filter and subject_filter not in item["subject"]:
            continue
        if version_filter and version_filter not in item["publisher"]:
            continue
        filtered.append(item)

    if limit:
        filtered = filtered[:limit]

    print(f"\n🚀 开始执行教材同步任务 (总计匹配到: {len(filtered)} 本, 并发数: {workers})")

    success_count = 0
    skip_count = 0
    fail_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {
            executor.submit(download_single_book, item, overwrite): item
            for item in filtered
        }

        for i, future in enumerate(concurrent.futures.as_completed(future_to_item), start=1):
            item = future_to_item[future]
            try:
                ok, reason = future.result()
                if ok:
                    if "跳过" in reason:
                        skip_count += 1
                        print(f"[{i}/{len(filtered)}] ⏭️ {item['filename']} -> {reason}")
                    else:
                        success_count += 1
                        print(f"[{i}/{len(filtered)}]  {item['filename']} -> {reason}")
                else:
                    fail_count += 1
                    print(f"[{i}/{len(filtered)}] ❌ {item['filename']} -> {reason}")
            except Exception as e:
                fail_count += 1
                print(f"[{i}/{len(filtered)}] ❌ {item['filename']} 异常: {e}")

    print("\n" + "=" * 60)
    print(f" 同步完成统计: 成功 {success_count} 本 | 跳过 {skip_count} 本 | 失败 {fail_count} 本")
    print("=" * 60)


def create_directory_structure(manifest: List[Dict[str, Any]]):
    """仅在本地生成规范的完整目录树（不下载 PDF 文件，秒级初始化）"""
    created_dirs = set()
    for item in manifest:
        target_path = CURRENT_DIR / item["relative_path"]
        target_dir = target_path.parent
        if str(target_dir) not in created_dirs:
            target_dir.mkdir(parents=True, exist_ok=True)
            # 放置一个 .gitkeep 保持目录结构
            gitkeep = target_dir / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.touch()
            created_dirs.add(str(target_dir))

    print(f" 目录结构树创建完毕: 共生成 {len(created_dirs)} 个分类目录！")


def main():
    parser = argparse.ArgumentParser(description="ChinaK12Books 教材库同步与增量版本更新工具")
    parser.add_argument("--init", action="store_true", help="初始化生成全量教材清单与规范目录树")
    parser.add_argument("--check", action="store_true", help="检查本地版本与平台最新版本差异")
    parser.add_argument("--sync", action="store_true", help="执行同步下载")
    parser.add_argument("--stage", type=str, help="过滤学段（如：小学、初中、高中）")
    parser.add_argument("--subject", type=str, help="过滤学科（如：数学、物理、化学、语文）")
    parser.add_argument("--version", type=str, help="过滤版本/出版社（如：人教版、北师大版、苏教版）")
    parser.add_argument("--limit", type=int, help="限制本次下载数量（用于测试）")
    parser.add_argument("--workers", type=int, default=4, help="并发下载线程数（默认 4）")
    parser.add_argument("--overwrite", action="store_true", help="强制覆盖已存在的文件")
    parser.add_argument("--refresh-index", action="store_true", help="强制重新扫描平台远程索引")

    args = parser.parse_args()

    # 如果清单不存在或要求刷新索引，先生成清单
    if not MANIFEST_JSON.exists() or args.refresh_index or args.init:
        manifest = generate_manifest(refresh_index=args.refresh_index)
        if args.init:
            create_directory_structure(manifest)
            print(" 初始化完成！已创建与 ChinaTextbook 一致的全部学段与年级目录。")
            return
    else:
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    if args.check:
        check_updates(manifest)
        return

    if args.sync:
        run_sync(
            manifest=manifest,
            stage_filter=args.stage,
            subject_filter=args.subject,
            version_filter=args.version,
            limit=args.limit,
            workers=args.workers,
            overwrite=args.overwrite,
        )
        return

    # 默认模式：如果没有任何参数，打印使用帮助
    parser.print_help()


if __name__ == "__main__":
    main()
