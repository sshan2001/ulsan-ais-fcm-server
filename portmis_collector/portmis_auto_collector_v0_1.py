#!/usr/bin/env python3
"""
Port-MIS Excel Auto Collector v0.1

This script opens the Port-MIS vessel entry/departure page, downloads an Excel
file for Ulsan Port, and uploads it to the existing Render server.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from dateutil.relativedelta import relativedelta
from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


TARGET_URL = (
    "https://new.portmis.go.kr/portmis/websquare/websquare.jsp"
    "?w2xPath=/portmis/w2/main/index.xml"
    "&page=/portmis/w2/sp/vssl/vsch/UI-PM-SP-104-02.xml"
    "&menuId=1319"
    "&menuCd=M0182"
    "&menuNm=%EC%84%A0%EB%B0%95%EC%9E%85%EC%B6%9C%ED%95%AD%ED%98%84%ED%99%A9"
)

DEFAULT_SERVER_URL = "https://ulsan-ais-fcm-server.onrender.com"
DEFAULT_API_KEY = "ulsan_ais_2026_mobile"
DEFAULT_DOWNLOAD_DIR = Path("tools/portmis_collector/downloads")
DEFAULT_DEBUG_DIR = Path("tools/portmis_collector/debug")
EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class CollectorError(RuntimeError):
    """Expected collector failure with a clear message."""

    def __init__(self, message: str, *, debug_saved: bool = False) -> None:
        super().__init__(message)
        self.debug_saved = debug_saved


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_") or "debug"


def save_debug(page: Optional[Page], debug_dir: Path, step: str) -> None:
    ensure_dir(debug_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = debug_dir / f"{stamp}_{safe_name(step)}"
    if page is None:
        log(f"디버그 저장 생략: page 객체가 없습니다. step={step}")
        return

    screenshot_path = prefix.with_suffix(".png")
    html_path = prefix.with_suffix(".html")
    try:
        page.screenshot(path=str(screenshot_path), full_page=True, timeout=15000)
        log(f"디버그 스크린샷 저장: {screenshot_path}")
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide original failure.
        log(f"디버그 스크린샷 저장 실패: {exc}")

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        log(f"디버그 HTML 저장: {html_path}")
    except Exception as exc:  # noqa: BLE001
        log(f"디버그 HTML 저장 실패: {exc}")


def format_portmis_date(value: date) -> str:
    # Port-MIS WebSquare inputs commonly accept YYYY-MM-DD.
    return value.strftime("%Y-%m-%d")


def frame_names(page: Page) -> str:
    names = []
    for frame in page.frames:
        names.append(frame.name or frame.url[:80] or "main")
    return ", ".join(names)


def wait_after_action(page: Page, milliseconds: int = 1200) -> None:
    page.wait_for_timeout(milliseconds)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass


def js_click_by_labels(frame: Any, labels: Iterable[str]) -> bool:
    return bool(
        frame.evaluate(
            """
            (labels) => {
              const wanted = labels.map((x) => String(x).toLowerCase());
              const elements = Array.from(document.querySelectorAll(
                'button, a, input[type=button], input[type=submit], span, div'
              ));
              function textOf(el) {
                return [
                  el.innerText,
                  el.textContent,
                  el.value,
                  el.title,
                  el.getAttribute('aria-label'),
                  el.getAttribute('alt')
                ].filter(Boolean).join(' ').toLowerCase();
              }
              for (const el of elements) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const text = textOf(el);
                if (!text) continue;
                if (wanted.some((label) => text.includes(label))) {
                  el.scrollIntoView({block: 'center', inline: 'center'});
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """,
            list(labels),
        )
    )


def click_by_labels(page: Page, labels: Iterable[str], step: str, required: bool = True) -> bool:
    label_list = list(labels)
    log(f"{step}: 클릭 후보 텍스트={label_list}")
    for frame in page.frames:
        for label in label_list:
            locators = [
                frame.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)),
                frame.get_by_text(label, exact=False),
                frame.locator(f"input[value*='{label}']"),
                frame.locator(f"[title*='{label}']"),
                frame.locator(f"[aria-label*='{label}']"),
            ]
            for locator in locators:
                try:
                    if locator.count() <= 0:
                        continue
                    target = locator.first()
                    target.scroll_into_view_if_needed(timeout=1500)
                    target.click(timeout=2500)
                    log(f"{step}: 클릭 성공 label={label}")
                    wait_after_action(page)
                    return True
                except Exception:
                    continue

        try:
            if js_click_by_labels(frame, label_list):
                log(f"{step}: JavaScript 클릭 성공")
                wait_after_action(page)
                return True
        except Exception:
            continue

    message = f"{step}: 클릭 대상 탐색 실패"
    if required:
        raise CollectorError(message)
    log(message)
    return False


def click_dom_id(page: Page, element_ids: Iterable[str], step: str) -> bool:
    ids = list(element_ids)
    log(f"{step}: exact DOM id click candidates={ids}")
    for frame in page.frames:
        try:
            clicked_id = frame.evaluate(
                """
                (ids) => {
                  for (const id of ids) {
                    const element = document.getElementById(id);
                    if (!element) continue;
                    const target = element.matches('a,button,input')
                      ? element
                      : (element.querySelector('a,button,input') || element);
                    element.scrollIntoView({block: 'center', inline: 'center'});
                    target.click();
                    return id;
                  }
                  return '';
                }
                """,
                ids,
            )
            if clicked_id:
                log(f"{step}: exact DOM id clicked={clicked_id}")
                wait_after_action(page)
                return True
        except Exception:
            continue
    return False


def set_input_value_with_events(frame: Any, selector: str, value: str) -> int:
    return int(
        frame.evaluate(
            """
            ([selector, value]) => {
              const nodes = Array.from(document.querySelectorAll(selector));
              let changed = 0;
              for (const el of nodes) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || el.disabled || el.readOnly) continue;
                el.focus();
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                changed += 1;
                break;
              }
              return changed;
            }
            """,
            [selector, value],
        )
    )


def set_websquare_input_calendar(frame: Any, component_id: str, input_id: str, display_value: str) -> bool:
    compact_value = display_value.replace("-", "")
    return bool(
        frame.evaluate(
            """
            ([componentId, inputId, displayValue, compactValue]) => {
              const input = document.getElementById(inputId);
              let changed = false;

              try {
                const comp = window.WebSquare?.util?.getComponentById(componentId);
                if (comp && typeof comp.setValue === 'function') {
                  comp.setValue(compactValue);
                  changed = true;
                }
              } catch (_) {}

              if (input) {
                input.scrollIntoView({block: 'center', inline: 'center'});
                input.focus();
                input.value = displayValue;
                for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                  input.dispatchEvent(new Event(eventName, {bubbles: true}));
                }
                changed = true;
              }

              return changed;
            }
            """,
            [component_id, input_id, display_value, compact_value],
        )
    )


def set_exact_portmis_dates(page: Page, start_text: str, end_text: str) -> bool:
    date_fields = [
        (
            "mf_tacMain_contents_M0182_body_srchBeginEtryndDt",
            "mf_tacMain_contents_M0182_body_srchBeginEtryndDt_input",
            start_text,
        ),
        (
            "mf_tacMain_contents_M0182_body_srchEndEtryndDt",
            "mf_tacMain_contents_M0182_body_srchEndEtryndDt_input",
            end_text,
        ),
    ]
    for frame in page.frames:
        changed = 0
        for component_id, input_id, value in date_fields:
            try:
                if set_websquare_input_calendar(frame, component_id, input_id, value):
                    changed += 1
            except Exception:
                pass
        if changed == len(date_fields):
            log("Date range set with exact Port-MIS input IDs.")
            wait_after_action(page, 500)
            return True
    return False


def fill_date_inputs(page: Page, start_text: str, end_text: str) -> None:
    log(f"조회 기간 설정 시도: {start_text} ~ {end_text}")
    if set_exact_portmis_dates(page, start_text, end_text):
        return

    start_selectors = [
        "#mf_tacMain_contents_M0182_body_srchBeginEtryndDt_input",
        "input[id*='start' i]",
        "input[id*='from' i]",
        "input[id*='sdate' i]",
        "input[id*='begin' i]",
        "input[id*='bgn' i]",
        "input[name*='start' i]",
        "input[name*='from' i]",
        "input[title*='시작']",
        "input[title*='시작일']",
        "input[aria-label*='시작']",
    ]
    end_selectors = [
        "#mf_tacMain_contents_M0182_body_srchEndEtryndDt_input",
        "input[id*='end' i]",
        "input[id*='to' i]",
        "input[id*='edate' i]",
        "input[id*='finish' i]",
        "input[name*='end' i]",
        "input[name*='to' i]",
        "input[title*='종료']",
        "input[title*='종료일']",
        "input[aria-label*='종료']",
    ]

    changed_start = 0
    changed_end = 0
    for frame in page.frames:
        for selector in start_selectors:
            try:
                changed_start += set_input_value_with_events(frame, selector, start_text)
                if changed_start:
                    break
            except Exception:
                continue
        for selector in end_selectors:
            try:
                changed_end += set_input_value_with_events(frame, selector, end_text)
                if changed_end:
                    break
            except Exception:
                continue

    if changed_start and changed_end:
        log("조회 기간 설정 성공: 명시적 selector 사용")
        wait_after_action(page, 500)
        return

    log("명시적 날짜 selector 실패. 날짜 입력칸 휴리스틱을 사용합니다.")
    changed_by_heuristic = 0
    for frame in page.frames:
        try:
            changed_by_heuristic += int(
                frame.evaluate(
                    """
                    ([startText, endText]) => {
                      const inputs = Array.from(document.querySelectorAll('input'));
                      function visible(el) {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && !el.disabled && !el.readOnly;
                      }
                      function textOf(el) {
                        return [
                          el.id,
                          el.name,
                          el.title,
                          el.placeholder,
                          el.getAttribute('aria-label'),
                          el.value
                        ].filter(Boolean).join(' ').toLowerCase();
                      }
                      function score(el) {
                        const text = textOf(el);
                        let value = 0;
                        if (/date|dt|ymd|일자|일시|날짜|기간|입항|출항/.test(text)) value += 5;
                        if (/yyyy|연도|월|일/.test(text)) value += 3;
                        if (/\\d{4}[-.]?\\d{2}[-.]?\\d{2}/.test(el.value || '')) value += 4;
                        if ((el.maxLength || 0) >= 8 && (el.maxLength || 0) <= 10) value += 1;
                        return value;
                      }
                      const candidates = inputs
                        .filter(visible)
                        .map((el) => ({el, score: score(el), text: textOf(el)}))
                        .filter((item) => item.score > 0)
                        .sort((a, b) => b.score - a.score);
                      if (candidates.length < 2) return 0;
                      const start = candidates.find((item) => /start|from|시작|부터|fr|sdate/.test(item.text)) || candidates[0];
                      const end = candidates.find((item) => /end|to|종료|까지|edate/.test(item.text) && item.el !== start.el) || candidates.find((item) => item.el !== start.el);
                      if (!start || !end) return 0;
                      for (const [el, value] of [[start.el, startText], [end.el, endText]]) {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        el.focus();
                        el.value = value;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                      }
                      return 2;
                    }
                    """,
                    [start_text, end_text],
                )
            )
            if changed_by_heuristic >= 2:
                break
        except Exception:
            continue

    if changed_by_heuristic < 2:
        raise CollectorError("조회 기간 입력칸을 찾지 못했습니다.")
    log("조회 기간 설정 성공: 휴리스틱 사용")
    wait_after_action(page, 500)


def select_ulsan_port(page: Page) -> None:
    log("Port agency filter attempt: Ulsan.")
    combo_id = "mf_tacMain_contents_M0182_body_chkSrchListPrtAgCd"
    button_id = f"{combo_id}_button"
    selected = False

    for frame in page.frames:
        try:
            combo = frame.locator(f"#{combo_id}")
            if combo.count() <= 0:
                continue
            combo.first().click(timeout=2500)
            page.wait_for_timeout(800)
            try:
                frame.evaluate(
                    """
                    (buttonId) => {
                      const button = document.getElementById(buttonId);
                      if (button) {
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        button.click();
                      }
                    }
                    """,
                    button_id,
                )
                page.wait_for_timeout(1000)
            except Exception:
                pass

            for label in (
                "820\uC6B8\uC0B0",
                "820 \uC6B8\uC0B0",
                "\uC6B8\uC0B0",
                "\uC6B8\uC0B0\uCCAD",
                "\uC6B8\uC0B0\uC9C0\uBC29\uD574\uC591\uC218\uC0B0\uCCAD",
            ):
                locator = frame.get_by_text(label, exact=False)
                if locator.count() <= 0:
                    continue
                locator.first().scroll_into_view_if_needed(timeout=1500)
                locator.first().click(timeout=2500)
                selected = True
                log(f"Port agency filter selected by visible text: {label}")
                break

            if not selected:
                selected = bool(
                    frame.evaluate(
                        """
                        ([comboId, buttonId]) => {
                          const button = document.getElementById(buttonId);
                          if (button) button.click();
                          const nodes = Array.from(document.querySelectorAll(
                            'label, span, div, td, li, a'
                          ));
                          const target = nodes.find((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || '').trim();
                            const compact = text.replace(/\\s+/g, '');
                            return rect.width > 0 && rect.height > 0 &&
                              (compact.includes('820\\uC6B8\\uC0B0') || text.includes('\\uC6B8\\uC0B0'));
                          });
                          if (!target) return false;
                          target.scrollIntoView({block: 'center', inline: 'center'});
                          target.click();
                          return true;
                        }
                        """,
                        [combo_id, button_id],
                    )
                )
                if selected:
                    log("Port agency filter selected by JavaScript visible text search.")
            if selected:
                break
        except Exception:
            continue

    if not selected:
        for frame in page.frames:
            try:
                selected = bool(
                    frame.evaluate(
                        """
                        (comboId) => {
                          let changed = false;
                          try {
                            const comp = window.WebSquare?.util?.getComponentById(comboId);
                            if (comp && typeof comp.setValue === 'function') {
                              comp.setValue('820');
                              changed = true;
                            }
                          } catch (_) {}

                          const label = document.getElementById(`${comboId}_label`);
                          if (label) {
                            label.textContent = '820울산';
                            changed = true;
                          }
                          const combo = document.getElementById(comboId);
                          if (combo) {
                            combo.dispatchEvent(new Event('change', {bubbles: true}));
                            combo.dispatchEvent(new Event('blur', {bubbles: true}));
                          }
                          return changed;
                        }
                        """,
                        combo_id,
                    )
                )
                if selected:
                    log("Port agency filter set by WebSquare value fallback: 820.")
                    break
            except Exception:
                continue

    if not selected:
        log("Ulsan agency selector was not found. Continuing without changing this filter.")
    wait_after_action(page, 700)


def set_max_rows(page: Page) -> bool:
    log("최대 표시 건수 설정 시도: 50000")
    exact_select_ids = [
        "mf_tacMain_contents_M0182_body_udcGridPageView_sbxRecordCount_input_0",
        "mf_tacMain_contents_M0182_body_udcGridPageView2_sbxRecordCount_input_0",
    ]
    for frame in page.frames:
        try:
            changed_id = frame.evaluate(
                """
                (ids) => {
                  function applySelect(select) {
                    if (!select) return false;
                    const options = Array.from(select.options || []);
                    const index = options.findIndex((option) => {
                      return String(option.text || '').includes('50000') ||
                             String(option.value || '').includes('50000');
                    });
                    if (index < 0) return false;
                    select.selectedIndex = index;
                    select.value = options[index].value;
                    for (const eventName of ['input', 'change']) {
                      select.dispatchEvent(new Event(eventName, {bubbles: true}));
                    }
                    return true;
                  }

                  for (const id of ids) {
                    const select = document.getElementById(id);
                    if (applySelect(select)) return id;
                  }

                  const selects = Array.from(document.querySelectorAll('select'));
                  for (const select of selects) {
                    if (applySelect(select)) return select.id || 'select-with-50000';
                  }
                  return '';
                }
                """,
                exact_select_ids,
            )
            if changed_id:
                log(f"최대 표시 건수 설정 성공: {changed_id}")
                wait_after_action(page, 900)
                return True
        except Exception:
            continue

    exact_selectors = [
        "#mf_tacMain_contents_M0182_body_udcGridPageView_sbxRecordCount_input_0",
        "#mf_tacMain_contents_M0182_body_udcGridPageView2_sbxRecordCount_input_0",
    ]
    for frame in page.frames:
        for selector in exact_selectors:
            try:
                select = frame.locator(selector)
                if select.count() <= 0:
                    continue
                select.first().select_option(label="50000개씩 보기", timeout=2500)
                log(f"최대 표시 건수 설정 성공: exact selector {selector}")
                wait_after_action(page, 700)
                return True
            except Exception:
                continue

    selected = False
    for frame in page.frames:
        try:
            selects = frame.locator("select")
            for index in range(selects.count()):
                select = selects.nth(index)
                options = select.locator("option")
                for option_index in range(options.count()):
                    option = options.nth(option_index)
                    text = option.inner_text(timeout=500).strip()
                    value = option.get_attribute("value", timeout=500) or ""
                    if "50000" in text or value == "50000":
                        if value:
                            select.select_option(value=value, timeout=2000)
                        else:
                            select.select_option(label=text, timeout=2000)
                        selected = True
                        log("최대 표시 건수 설정 성공: 50000")
                        break
                if selected:
                    break
        except Exception:
            pass
        if selected:
            break

    if not selected:
        log("50000개 표시 selector를 찾지 못했습니다. 현재 기본 표시 건수로 계속 진행합니다.")
    wait_after_action(page, 700)
    return selected


def get_result_count(page: Page) -> Optional[int]:
    selectors = [
        "#mf_tacMain_contents_M0182_body_udcGridPageView_txtTotalDataCount",
        "#mf_tacMain_contents_M0182_body_udcGridPageView2_txtTotalDataCount",
    ]
    texts: list[str] = []
    for frame in page.frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                if locator.count() <= 0:
                    continue
                text = locator.first().inner_text(timeout=1000).strip()
                if text:
                    texts.append(text)
            except Exception:
                continue
        try:
            texts.extend(
                frame.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('span, div'))
                      .map((el) => (el.innerText || el.textContent || '').trim())
                      .filter((text) => /^\\([^)]*[0-9,]+[^)]*\\)$/.test(text) && text.length <= 30)
                      .slice(0, 4)
                    """
                )
            )
        except Exception:
            pass

    counts: list[int] = []
    for text in texts:
        match = re.search(r"([0-9][0-9,]*)", text)
        if match:
            counts.append(int(match.group(1).replace(",", "")))
    return max(counts) if counts else None


def ensure_search_has_results(page: Page, debug_dir: Path) -> None:
    count = get_result_count(page)
    if count is None:
        log("Search result count was not detected. Continuing to Excel download step.")
        return
    log(f"Search result count detected: {count}")
    if count <= 0:
        save_debug(page, debug_dir, "search_result_zero")
        raise CollectorError(
            "Search result is 0. Check the date range and Port agency filter in the debug screenshot.",
            debug_saved=True,
        )


def click_exact_search(page: Page) -> bool:
    ids = [
        "mf_tacMain_contents_M0182_body_btnSrch_btnSearch",
        "mf_tacMain_contents_M0182_body_btnSrch2_btnSearch",
    ]
    if click_dom_id(page, ids, "조회 버튼"):
        return True

    selectors = [f"#{element_id}" for element_id in ids]
    for frame in page.frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                if locator.count() <= 0:
                    continue
                target = locator.first()
                if not target.is_visible(timeout=1000):
                    continue
                target.scroll_into_view_if_needed(timeout=1500)
                target.click(timeout=3000)
                log(f"Search clicked by exact selector: {selector}")
                wait_after_action(page)
                return True
            except Exception:
                continue
    return False


def click_exact_excel_download(page: Page) -> bool:
    ids = [
        "mf_tacMain_contents_M0182_body_btnUdcCommon_btnDownloadExcel",
        "mf_tacMain_contents_M0182_body_btnUdcCommon2_btnDownloadExcel",
        "mf_tacMain_contents_M0182_body_btn_excel_down",
    ]
    if click_dom_id(page, ids, "엑셀 다운로드"):
        return True

    selectors = [f"#{element_id}" for element_id in ids]
    for frame in page.frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                if locator.count() <= 0:
                    continue
                target = locator.first()
                if not target.is_visible(timeout=1000):
                    continue
                target.scroll_into_view_if_needed(timeout=1500)
                target.click(timeout=3000)
                log(f"Excel download clicked by exact selector: {selector}")
                return True
            except Exception:
                continue
    return False


def download_excel(page: Page, download_dir: Path, debug_dir: Path, timeout_ms: int) -> Path:
    ensure_dir(download_dir)
    log("엑셀 다운로드 버튼 클릭 시도")
    try:
        with page.expect_download(timeout=timeout_ms) as download_info:
            if not click_exact_excel_download(page):
                click_by_labels(page, ["엑셀", "Excel", "EXCEL", "다운로드", "Download"], "엑셀 다운로드")
        download = download_info.value
    except Exception as exc:
        save_debug(page, debug_dir, "excel_download_failed")
        raise CollectorError(f"엑셀 다운로드 실패: {exc}", debug_saved=True) from exc

    suggested = download.suggested_filename or f"portmis_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    if not suggested.lower().endswith(".xlsx"):
        save_debug(page, debug_dir, "download_not_xlsx")
        raise CollectorError(f"다운로드 파일이 xlsx가 아닙니다: {suggested}")

    target = download_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name(suggested)}"
    download.save_as(str(target))
    if not target.exists() or target.stat().st_size <= 0:
        save_debug(page, debug_dir, "download_empty")
        raise CollectorError(f"다운로드 파일 저장 실패 또는 빈 파일: {target}")
    log(f"엑셀 다운로드 완료: {target} ({target.stat().st_size:,} bytes)")
    return target


def upload_excel(server_url: str, api_key: str, xlsx_path: Path) -> dict[str, Any]:
    upload_url = f"{server_url.rstrip('/')}/portmis/upload-excel"
    log(f"Render 서버 업로드 시작: {upload_url}")
    headers = {"X-API-Key": api_key}
    params = {"api_key": api_key}
    with xlsx_path.open("rb") as handle:
        response = requests.post(
            upload_url,
            headers=headers,
            params=params,
            files={"file": (xlsx_path.name, handle, EXCEL_MIME)},
            timeout=180,
        )
    log(f"업로드 HTTP 상태: {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CollectorError(f"업로드 응답이 JSON이 아닙니다: {response.text[:1000]}") from exc
    if response.status_code >= 400:
        raise CollectorError(f"업로드 실패: HTTP {response.status_code}\n{pretty_json(data)}")
    log("업로드 응답 JSON:")
    print(pretty_json(data), flush=True)
    return data


def fetch_status(server_url: str, api_key: str) -> dict[str, Any]:
    status_url = f"{server_url.rstrip('/')}/portmis/status"
    log(f"업로드 상태 확인: {status_url}")
    response = requests.get(
        status_url,
        headers={"X-API-Key": api_key},
        params={"api_key": api_key},
        timeout=60,
    )
    log(f"상태 확인 HTTP 상태: {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CollectorError(f"상태 응답이 JSON이 아닙니다: {response.text[:1000]}") from exc
    print(pretty_json(data), flush=True)
    return data


def print_upload_summary(data: dict[str, Any]) -> None:
    log("업로드 요약:")
    candidates = [data]
    for key in ("summary", "data", "result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for name in ("count", "from", "to", "portCounts", "movementCounts"):
        value = None
        for item in candidates:
            if name in item:
                value = item[name]
                break
        if value is not None:
            print(f"  - {name}: {pretty_json(value) if isinstance(value, (dict, list)) else value}", flush=True)


def run_collector(args: argparse.Namespace) -> int:
    download_dir = ensure_dir(Path(args.download_dir))
    debug_dir = ensure_dir(Path(args.debug_dir))
    start_day = date.today()
    end_day = start_day + relativedelta(days=args.days)
    start_text = format_portmis_date(start_day)
    end_text = format_portmis_date(end_day)

    log("Port-MIS Excel Auto Collector v0.1 시작")
    log(f"조회 기간: {start_text} ~ {end_text}")
    log(f"브라우저 모드: {'headless' if args.headless else 'headful'}")
    log(f"다운로드 폴더: {download_dir}")
    log(f"디버그 폴더: {debug_dir}")

    page: Optional[Page] = None
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=args.headless,
                slow_mo=150 if not args.headless else 0,
            )
            context = browser.new_context(accept_downloads=True, locale="ko-KR")
            page = context.new_page()
            page.set_default_timeout(args.timeout_ms)

            log(f"Port-MIS 페이지 열기: {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=args.timeout_ms)
            wait_after_action(page, 5000)
            log(f"페이지 로드 완료. frames={frame_names(page)}")

            fill_date_inputs(page, start_text, end_text)
            select_ulsan_port(page)
            set_max_rows(page)
            if not click_exact_search(page):
                click_by_labels(page, ["조회", "검색"], "조회 버튼")
            log("조회 결과 로딩 대기")
            wait_after_action(page, 6000)
            set_max_rows(page)
            ensure_search_has_results(page, debug_dir)

            xlsx_path = download_excel(page, download_dir, debug_dir, args.download_timeout_ms)
            if args.no_upload:
                log("--no-upload 옵션이 있어 서버 업로드를 건너뜁니다.")
                log(f"다운로드 파일: {xlsx_path}")
                return 0

            upload_data = upload_excel(args.server_url, args.api_key, xlsx_path)
            print_upload_summary(upload_data)
            fetch_status(args.server_url, args.api_key)
            log("Port-MIS 자동 수집 완료")
            return 0
    except Exception as exc:
        log(f"오류 발생: {exc}")
        if not getattr(exc, "debug_saved", False):
            save_debug(page, debug_dir, "collector_failed")
        return 1
    finally:
        try:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Port-MIS Excel Auto Collector v0.1")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headful", dest="headless", action="store_false", help="브라우저를 보이게 실행합니다. 기본값입니다.")
    mode.add_argument("--headless", dest="headless", action="store_true", help="브라우저를 숨기고 실행합니다.")
    parser.set_defaults(headless=False)

    parser.add_argument("--days", type=int, default=7, help="오늘부터 며칠 후까지 조회할지 지정합니다. 기본값: 7")
    parser.add_argument("--no-upload", action="store_true", help="엑셀 다운로드만 하고 서버 업로드는 하지 않습니다.")
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL, help=f"Render 서버 URL. 기본값: {DEFAULT_SERVER_URL}")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="Render 서버 API key")
    parser.add_argument("--download-dir", default=str(DEFAULT_DOWNLOAD_DIR), help=f"다운로드 폴더. 기본값: {DEFAULT_DOWNLOAD_DIR}")
    parser.add_argument("--debug-dir", default=str(DEFAULT_DEBUG_DIR), help=f"디버그 저장 폴더. 기본값: {DEFAULT_DEBUG_DIR}")
    parser.add_argument("--timeout-ms", type=int, default=45000, help="Playwright 기본 대기 시간(ms). 기본값: 45000")
    parser.add_argument("--download-timeout-ms", type=int, default=90000, help="엑셀 다운로드 대기 시간(ms). 기본값: 90000")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.days < 0:
        parser.error("--days 값은 0 이상이어야 합니다.")
    return run_collector(args)


if __name__ == "__main__":
    sys.exit(main())
