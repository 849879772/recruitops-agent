"""Deterministic validation of model-selected persisted page evidence.

Source references are server-created array indices, never model-created DOM.
The model may locate evidence; it cannot invent identity or a current marker.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from packages.domain.application_evidence_text import evidence_spans, evidence_text_key, localize_evidence
from packages.domain.application_identity import clean_display_title, displayed_cities, is_application_title, matching_records, recruitment_titles_agree, title_span_is_complete
from packages.domain.application_status_semantics import (
    asserted_submission_label, current_status_labels, explicit_label_status, status_is_unasserted,
    timeline_without_current, literal_record_status, noncanonical_status_reason, status_evidence_conflict,
    dated_submission_label, is_resume_routing_status,
    literal_current_status_labels, literal_current_status_conflict,
)


def page_sources(observation: Mapping, *, visual: bool = False) -> list[dict]:
    if visual:
        reading = observation.get("vision") or {}
        text = str(reading.get("text") or "") if isinstance(reading, Mapping) else ""
        if not text:
            return []
        bounded = text[:12000]
        sources = [{"ref": "vision:text", "text": bounded}]
        cards = reading.get("cards") if reading.get("reading_version") == "literal-cards-v1" else None
        for index, card in enumerate(cards if isinstance(cards, list) else []):
            if not isinstance(card, Mapping):
                continue
            raw = card.get("text")
            title, label = card.get("title"), card.get("current_label")
            if (not isinstance(raw, str) or len(raw) > 2000 or not raw.strip()
                    or not isinstance(title, str) or not title.strip()
                    or localize_evidence(raw, title) is None
                    or not isinstance(label, str) or not isinstance(card.get("current"), bool)
                    or (label and localize_evidence(raw, label) is None)
                    or (card["current"] and not label)
                    or localize_evidence(bounded, raw) is None):
                continue
            source = {"ref": f"vision:card:{index}", "text": raw, "title": title,
                      "current_label": label, "current": card["current"], "scoped": True}
            identity_title = _corroborated_ocr_title(observation, title, raw)
            if identity_title:
                source["identity_title"] = identity_title
            sources.append(source)
        if not cards:
            # Legacy readings contain no machine-readable card boundary. A blank
            # line is useful for selection, but never proves an active status.
            sources.extend({"ref": f"vision:block:{index}", "text": block, "scoped": "legacy_block"}
                           for index, block in enumerate(re.split(r"\n\s*\n", bounded)) if block.strip())
        for index, line in enumerate(bounded.splitlines()):
            if not line.strip():
                continue
            parents = [source["ref"] for source in sources[1:]
                       if source["ref"].startswith(("vision:card:", "vision:block:"))
                       and localize_evidence(source["text"], line) is not None]
            sources.append({"ref": f"vision:line:{index}", "text": line,
                            **({"parent_ref": parents[0]} if len(parents) == 1 else {})})
        return sources
    page = observation.get("page") or {}
    text = str(page.get("text") or "") if isinstance(page, Mapping) else ""
    sources = [{"ref": "page:text", "text": text[:20000]}] if text else []
    for index, segment in enumerate((observation.get("page_segments") or [])[:32]):
        if not isinstance(segment, Mapping) or not str(segment.get("text") or "").strip():
            continue
        sources.append({"ref": f"frame:{segment.get('frameId', index)}:text", "text": str(segment["text"])[:20000],
                        "frameId": segment.get("frameId", index)})
    for index, node in enumerate((observation.get("semantic_nodes") or [])[:240]):
        if not isinstance(node, Mapping) or not str(node.get("text") or "").strip():
            continue
        sources.append({"ref": f"node:{index}", "text": str(node["text"])[:2000],
                        **{key: node[key] for key in ("tag", "role", "attributes", "classTokens", "visual", "rect", "frameId") if key in node}})
    return sources


def _ocr_title_variant(visible, dom):
    """Closed OCR repairs; never use this as a global identity comparator."""
    key, other = evidence_text_key(visible).casefold(), evidence_text_key(dom).casefold()
    differences = [(a, b) for a, b in zip(key, other) if a != b]
    if len(key) == len(other) and len(differences) == 1 and set(differences[0]) == {"i", "l"}:
        return True
    # An extra bracket pair around one in-word CJK glyph, not a qualifier.
    bracket = list(re.finditer(r"(?<=[\u4e00-\u9fff])【([\u4e00-\u9fff])】", key))
    if len(bracket) == 1:
        match = bracket[0]
        if key[:match.start()] + match[1] + key[match.end():] == other:
            return True
    # A bilingual title independently repeats its complete Chinese role. Only
    # one long ASCII word may have at most two substituted characters; all
    # other words, qualifiers, city suffixes and IDs remain exact.
    pattern = r"([A-Za-z]+(?:\s+[A-Za-z]+)*)\s*(\([^()]*[\u4e00-\u9fff][^()]*\))"
    left = re.fullmatch(pattern, unicodedata.normalize("NFKC", visible).strip())
    right = re.fullmatch(pattern, unicodedata.normalize("NFKC", dom).strip())
    if not left or not right or evidence_text_key(left[2]) != evidence_text_key(right[2]):
        return False
    words, other_words = left[1].casefold().split(), right[1].casefold().split()
    changed = [(a, b) for a, b in zip(words, other_words) if a != b]
    return (len(words) == len(other_words) and len(changed) == 1
            and len(changed[0][0]) == len(changed[0][1]) >= 6
            and sum(a != b for a, b in zip(*changed[0])) <= 2)


def _bilingual_dom_anchors(observation, title):
    """Read one dated submission block when the DOM parser omitted cards."""
    normalized = unicodedata.normalize("NFKC", title).strip()
    match = re.fullmatch(r"([A-Za-z]+(?:\s+[A-Za-z]+)*)\s*(\([^()]*[\u4e00-\u9fff][^()]*\))", normalized)
    if not match:
        return []
    suffix = r"\s*".join(re.escape(char) for char in match[2] if not char.isspace())
    english = r"(?:[A-Za-z]+\s+){" + str(len(match[1].split()) - 1) + r"}[A-Za-z]+\s*"
    pattern = re.compile(r"(?<![A-Za-z])(?P<title>" + english + suffix + r")"
                         r"\s+(?:官网(?:主)?投递|内推投递)\s+"
                         r"(?:(?!官网(?:主)?投递|内推投递|投递简历).){0,300}?"
                         r"投递简历\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}", re.S)
    anchors, seen = [], set()
    for source in page_sources(observation):
        if source["ref"] != "page:text" and not source["ref"].startswith("frame:"):
            continue
        text = unicodedata.normalize("NFKC", source["text"])
        key = evidence_text_key(text)
        if key in seen:
            continue
        seen.add(key)
        anchors.extend({"title": item["title"], "context": item[0]} for item in pattern.finditer(text))
    return anchors


def _corroborated_ocr_title(observation, title, text):
    """Repair bounded OCR variants only with a unique same-observation DOM anchor.

    This is not a global fuzzy identity rule. Cities, role suffixes, identifiers
    and digits remain exact, and the original OCR quotation stays unchanged.
    """
    # A title field can omit a displayed volunteer tag; recover it only from
    # the same literal card, never from another card or the page-wide reading.
    volunteer = r"(?:网申)?第\s*(?P<number>[一二三四五六七八九十\d]+)\s*志愿"
    def volunteer_numbers(value):
        return {str(int(number)) if number.isdigit() else str({
            "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }.get(number, number)) for number in re.findall(volunteer, value)}
    def core(value):
        value = re.sub(r"^" + volunteer + r"\s+", "", value)
        return re.sub(r"\s*" + volunteer + r"\s*$", "", value)
    candidates = []
    records = [card for card in observation.get("application_records") or [] if isinstance(card, Mapping)]
    # A parser omission is recoverable only from a literal, bounded, dated
    # personal submission in this capture; it never supplies visual status.
    if not records:
        records = _bilingual_dom_anchors(observation, title)
    # If the DOM also contains the literal visual title, it could be a real
    # second role. A nearby spelling with shared dates is not authority to merge.
    if any(evidence_text_key(core(str(card.get("raw_title") or card.get("title") or "")))
           == evidence_text_key(core(title)) for card in records):
        return None
    for card in records:
        if not isinstance(card, Mapping):
            continue
        raw_title = str(card.get("raw_title") or card.get("title") or "")
        if not _ocr_title_variant(core(title), core(raw_title)):
            continue
        context = str(card.get("context") or card.get("evidence") or "")
        dom_volunteers = volunteer_numbers(raw_title)
        if card.get("volunteer_index"):
            dom_volunteers.add(str(card["volunteer_index"]).strip())
        visual_volunteers = volunteer_numbers(title)
        visual_text_volunteers = volunteer_numbers(text)
        if dom_volunteers or visual_volunteers:
            if (len(dom_volunteers) != 1 or visual_text_volunteers != dom_volunteers
                    or visual_volunteers and visual_volunteers != dom_volunteers):
                continue
        anchors = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?", context)
        visual_dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?", text)
        if anchors and visual_dates and set(anchors) != set(visual_dates):
            continue
        location = r"(?:意向城市|工作地点|岗位城市)\s*[:：]\s*(?:\d+\s+)?([^\s,，;；|]+)"
        dom_locations, visual_locations = set(re.findall(location, context)), set(re.findall(location, text))
        if dom_locations and visual_locations and dom_locations != visual_locations:
            continue
        dom_cities, visual_cities = displayed_cities(context), displayed_cities(text)
        if dom_cities and visual_cities and dom_cities != visual_cities:
            continue
        id_conflict = False
        for field, prefix in (("job_id", r"(?:岗位|职位)(?:编号|ID)|job[_ ]?id"),
                              ("application_id", r"(?:投递|申请)(?:编号|ID)|application[_ ]?id")):
            visible_ids = re.findall(r"(?:" + prefix + r")\s*[:：#]\s*([\w-]+)", text, re.I)
            dom_ids = re.findall(r"(?:" + prefix + r")\s*[:：#]\s*([\w-]+)", context, re.I)
            if card.get(field):
                dom_ids.append(str(card[field]))
            if dom_ids and visible_ids and {value.casefold() for value in dom_ids} != {
                    value.casefold() for value in visible_ids}:
                id_conflict = True
        if id_conflict:
            continue
        anchors.extend(str(card[field]) for field in ("job_id", "application_id")
                       if card.get(field) and len(str(card[field])) >= 4)
        if any(localize_evidence(text, anchor) for anchor in anchors):
            candidates.append(raw_title)
    return candidates[0] if len(candidates) == 1 else None


def _value(app, field):
    return str((app.get(field) if isinstance(app, Mapping) else getattr(app, field, "")) or "")


def _visual_title_agrees(visible, title):
    if evidence_text_key(visible) == evidence_text_key(title):
        return True
    if recruitment_titles_agree(visible, title):
        return True
    # A scoped screenshot may absorb an adjacent recruitment tag. Keep the
    # vocabulary closed; never drop city, job ID or an arbitrary role suffix.
    cohort = r"^[【\[](?:20)?\d{2}(?:届)?校招(?:-联合动力)?[】\]]\s*"
    if re.match(cohort, visible) and evidence_text_key(re.sub(cohort, '', visible)) == evidence_text_key(title):
        return True
    volunteer = r"\s*(?:网申)?第[一二三四五六七八九十\d]+志愿\s*$"
    if (re.search(volunteer, visible) and not re.search(volunteer, title)
            and clean_display_title(visible) == clean_display_title(title)):
        return True
    prefix = r"^(?:网申)?第[一二三四五六七八九十\d]+志愿\s+"
    if (re.match(prefix, visible) and not re.search(r"第[一二三四五六七八九十\d]+志愿", title)
            and evidence_text_key(re.sub(prefix, "", visible)) == evidence_text_key(title)):
        return True
    # An OCR title field can mistakenly hold the card's recruitment heading.
    # The actual job title is independently required inside this same card and
    # uniquely bound above; only this closed heading form is recoverable.
    if re.fullmatch(r"(?:20)?\d{2}届(?:应届生)?(?:校园招聘|校招)", visible.strip()):
        return True
    # OCR may include adjacent employment/category badges in the title field.
    # Strip only a delimited closed vocabulary, never a city or another role.
    parts = re.split(r"[|｜]", visible)
    return (len(parts) > 1 and evidence_text_key(parts[0]) == evidence_text_key(title)
            and all(part.strip() in {"应届生", "软件类", "技术类", "全职", "校园招聘", "校招"}
                    for part in parts[1:]))


def _active(node):
    attrs = node.get("attributes") or {}
    return (str(attrs.get("aria-current") or "").casefold() in {"true", "step", "page"}
            or str(attrs.get("aria-selected") or "").casefold() == "true"
            or any(re.search(r"(?:^|[-_])(?:current|active)(?:$|[-_])", str(token), re.I)
                   for token in node.get("classTokens") or []))


def _inside(node, container):
    if node.get("frameId") != container.get("frameId"):
        return False
    a, b = node.get("rect") or {}, container.get("rect") or {}
    try:
        return (b["x"] <= a["x"] and b["y"] <= a["y"] and a["width"] > 0 and a["height"] > 0
                and a["x"] + a["width"] <= b["x"] + b["width"]
                and a["y"] + a["height"] <= b["y"] + b["height"])
    except (KeyError, TypeError):
        return False


def _title_spans(text, title):
    # Keep word/title suffix boundaries in the original text. Removing spaces
    # first would wrongly join a real title to the location/body after it.
    return [(start, end) for start, end in evidence_spans(text, title)
            if title_span_is_complete(text, start, end)]


_NAMED_SUBMISSION = re.compile(
    r"您已成功投递【(?P<title>[^【】\n]{1,512})】岗位"
    r"(?:[，,]\s*您所选择的工作地点为【[^【】\n]{1,120}】)?"
    r"(?:[，,]\s*简历评估中[.…]*)?"
)


def identity_fallback_cards(observation, *, verified_vision=False):
    """Recover literal identity choices, never model-created aliases or stages.

    The caller must verify the saved vision audit. Repeated cards within one
    source stay separate; duplicated top-frame text is not a second source.
    """
    if observation.get("diagnostics_compacted_v1"):
        return []
    if verified_vision:
        cards = [{"title": source["title"], "raw_title": source["title"],
                  "context": source["text"], "evidence_source": "vision",
                  "evidence_ref": source["ref"]}
                 for source in page_sources(observation, visual=True)
                 if source["ref"].startswith("vision:card:")]
        if cards:
            return cards
    # A personal, explicitly named submission receipt is not a generic Apply
    # button. Do not infer titles from arbitrary prose or from a model response.
    cards, seen_sources = [], set()
    for source in page_sources(observation):
        if source["ref"] != "page:text" and not source["ref"].startswith("frame:"):
            continue
        key = evidence_text_key(source["text"])
        if key in seen_sources:
            continue
        seen_sources.add(key)
        for match in _NAMED_SUBMISSION.finditer(source["text"]):
            title = match["title"].strip()
            if title:
                cards.append({"title": title, "raw_title": title, "context": match[0],
                              "evidence_source": "page_text", "evidence_ref": source["ref"]})
    return cards


def _unique_named_submission(source, quotation, title, label):
    matches = list(_NAMED_SUBMISSION.finditer(source["text"]))
    # Only an applied baseline from exactly one named receipt. Two same-name
    # applications, even with identical text, still require identity resolution.
    spans = _title_spans(source["text"], title)
    if len(matches) != 1 or not 1 <= len(spans) <= 2:
        return False
    preceding = literal_record_status(source["text"][spans[0][0]:matches[0].start()])
    if preceding and preceding[0] != "applied":
        return False
    return (evidence_text_key(matches[0]["title"]) == evidence_text_key(title)
            and localize_evidence(quotation, matches[0][0]) is not None
            and localize_evidence(matches[0][0], label) is not None)


def _standalone_current_badge(sources, source, matches, label):
    """Recognize an ongoing status badge inside a uniquely bound DOM card.

    Some ATS cards have a simple ``初筛中`` badge, not an active process step
    or a ``当前状态:`` prefix. Accept only a closed set of asserted ongoing
    labels in a non-interactive child node. Submission metadata may surround
    that badge; arbitrary body text, dated history and stage ladders may not.
    """
    ongoing = r"(?:简历)?(?:初筛|筛选|测评|笔试|面试|复试|终面)(?:进行)?中"
    if (not source["ref"].startswith("node:") or len(matches) != 1
            or timeline_without_current(matches[0])
            or (matches[0].get("signals") or {}).get("has_progress_timeline")
            or not re.fullmatch(ongoing, label)):
        return False
    context = str(matches[0].get("context") or matches[0].get("evidence") or "")
    if not localize_evidence(context, source["text"]):
        return False
    metadata = (r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+)?"
                r"(?:(?:投递|申请|官网主投|官网投递|官网|内推|网申)\s+)+")
    labels = set()
    for node in sources:
        if (not node["ref"].startswith("node:") or not _inside(node, source)
                or str(node.get("tag") or "").casefold() in {"a", "button", "input", "select", "option"}
                or str(node.get("role") or "").casefold() in {"button", "link", "menuitem", "option"}):
            continue
        found = re.fullmatch(r"(?:" + metadata + r")?(?P<label>" + ongoing + r")", node["text"].strip())
        if found:
            labels.add(found.group("label"))
    return label in labels and {explicit_label_status(value) for value in labels} == {explicit_label_status(label)}


def validate_page_candidate(observation, application, page_applications, *, card_title,
                            source_ref, quotation, label, status, current, current_node_ref=None,
                            visual=False):
    """Return a verified source card or an explicit non-writing reason."""
    sources = page_sources(observation, visual=visual)
    source = next((item for item in sources if item["ref"] == source_ref), None)
    if source is None:
        return None, "model_quote_not_found"
    quotation = localize_evidence(source["text"], quotation)
    label = localize_evidence(quotation, label) if quotation else None
    if not quotation or not label:
        return None, "model_quote_not_found"
    noncanonical = noncanonical_status_reason(label) or (
        "record_present_status_unknown" if is_resume_routing_status(label) else None)
    if explicit_label_status(label) != status and not noncanonical:
        return None, "status_semantics_unsupported"
    supplied_title = card_title
    card_title = localize_evidence(quotation, card_title) if card_title else None
    if (source.get("scoped") is True and source_ref.startswith("vision:card:")
            and source.get("title") and (recruitment_titles_agree(source["title"], supplied_title)
                or source.get("identity_title") and recruitment_titles_agree(source["identity_title"], supplied_title))):
        # An audited screenshot card owns its literal full title. A model may
        # copy the local title without a closed display attribute such as
        # “接受调剂”; restore the source title, never erase arbitrary qualifiers.
        visible_title = localize_evidence(quotation, source["title"])
        if visible_title:
            card_title = visible_title
    if not card_title:
        # Missing a title in the selected excerpt is not a different job. Keep
        # that distinction visible instead of asking for an identity approval.
        if supplied_title and localize_evidence(source["text"], supplied_title):
            return None, "model_evidence_scope_incomplete"
        return None, "model_identity_mismatch"
    if not _title_spans(source["text"], card_title):
        return None, "model_identity_mismatch"
    if re.search(r"ignore\s+(?:all\s+)?(?:previous|prior|system)|system\s*prompt|(?:忽略|无视).{0,12}(?:指令|提示|规则)", quotation, re.I):
        return None, "untrusted_web_content"
    records = [item for item in observation.get("application_records") or [] if isinstance(item, Mapping)
               and is_application_title(item.get("raw_title") or item.get("title"))]
    matches = matching_records(application, records)
    if len(matches) > 1:
        return None, "target_record_ambiguous"
    named_submission = (not visual and status == "applied"
                        and _unique_named_submission(source, quotation, card_title, label))
    if not matches and any(len(_title_spans(item["text"], card_title)) > 1
                           and not (named_submission and _unique_named_submission(item, quotation, card_title, label))
                           for item in sources if item["ref"] == "page:text" or item["ref"].startswith(("frame:", "vision:"))):
        return None, "target_record_ambiguous"
    # Reuse persisted stable identifiers only when the exact observed title agrees.
    identity_title = source.get("identity_title") or card_title
    identity = dict(matches[0]) if len(matches) == 1 and evidence_text_key(identity_title) in {
        evidence_text_key(str(matches[0].get("title") or "")), evidence_text_key(str(matches[0].get("raw_title") or ""))
    } else {"title": identity_title, "raw_title": identity_title}
    if not matching_records(application, [identity]):
        return None, "model_identity_mismatch"
    owners = [app for app in page_applications if matching_records(app, [identity])]
    if len(owners) != 1 or _value(owners[0], "id") != _value(application, "id"):
        return None, "target_record_ambiguous"
    if matches and status_evidence_conflict(matches[0]):
        return None, "status_evidence_conflict"
    if matches and any(card not in matches and evidence_text_key(str(card.get("raw_title") or card.get("title") or ""))
                       == evidence_text_key(card_title) for card in records):
        bound_context = str(matches[0].get("context") or matches[0].get("evidence") or "")
        if not localize_evidence(bound_context, quotation):
            return None, "target_record_ambiguous"
    # An exact quote cannot borrow an outcome from a different known title/card.
    other_titles = {_value(app, "job_title") for app in page_applications if _value(app, "id") != _value(application, "id")}
    other_titles.update(str(card.get("raw_title") or card.get("title") or "") for card in records if card not in matches)
    if any(title and evidence_text_key(title) != evidence_text_key(card_title)
           and _title_spans(quotation, title) for title in other_titles):
        return None, "target_record_ambiguous"
    if source.get("scoped") is True and source_ref.startswith("vision:card:") and literal_current_status_conflict(source["text"]):
        return None, "status_evidence_conflict"
    if noncanonical:
        # Meaningful generic wording is not a model fault or a request to bind
        # another job. Identity and literal quotation were still checked above.
        return None, noncanonical
    if source.get("scoped") is True and source_ref.startswith("vision:card:"):
        if any(noncanonical_status_reason(value) or is_resume_routing_status(value)
               for value in literal_current_status_labels(source["text"])):
            return None, "record_present_status_unknown"
    # Other cards elsewhere on the page do not invalidate this one. The quote
    # must stay local to this title, and the current assertion below must be
    # adjacent to it; an intervening unknown card title cannot lend its status.
    if (source_ref == "page:text" or source_ref.startswith(("vision:", "frame:"))) and not source_ref.startswith("vision:card:"):
        title_end = quotation.find(card_title) + len(card_title)
        label_start = quotation.find(label, title_end)
        if label_start < title_end or label_start - title_end > 160:
            return None, "model_quote_not_found"
    asserted = bool(re.search(re.escape(card_title) + r"[\s:：|·\-]{0,20}(?:当前进度|申请进度|应聘进度|当前状态|目前状态|最新状态|状态|status)\s*[:：]\s*" + re.escape(label), quotation, re.I))
    scoped_visual = source.get("scoped") is True and source_ref.startswith("vision:card:")
    literal = literal_record_status(source['text']) if scoped_visual else None
    literal_asserted = (literal is not None and literal[0] == status
                        and localize_evidence(literal[1], label) is not None)
    visual_current = (scoped_visual and source.get("current") is True
                      and evidence_text_key(source.get("current_label", "")) == evidence_text_key(label)
                      and _visual_title_agrees(source.get("title", ""), card_title))
    if scoped_visual:
        # The new reader never adds a synthetic 'current status:' line to OCR.
        # The visual active-node interpretation is an explicit separate field.
        asserted = visual_current or literal_asserted
    elif source_ref.startswith("vision:") and not (observation.get("vision") or {}).get("reading_version"):
        # For old captures, allow only tightly delimited recruitment metadata
        # between the job name and explicit state, not arbitrary nearby prose.
        metadata = r"(?:[\s:：|｜·\-]|第[一二三四五六七八九十\d]+志愿|官网投递|应届生|软件类|意向岗位[:：][^\n]{0,30}|意向城市[:：][^\n]{0,60}){0,30}"
        asserted = bool(re.search(re.escape(card_title) + metadata + r"(?:当前状态|状态)\s*[:：]\s*" + re.escape(label), quotation))
    baseline = named_submission or (status == "applied" and bool(asserted_submission_label(label))
                and bool(re.search(re.escape(card_title) + r"[\s:：|·\-]{0,20}" + re.escape(label), quotation)))
    active = next((item for item in sources if item["ref"] == current_node_ref), None)
    marker = bool(active and _active(active) and evidence_text_key(active["text"]) == evidence_text_key(label)
                  and (active is source or _inside(active, source)))
    badge = _standalone_current_badge(sources, source, matches, label)
    if marker:
        active_stages = {explicit_label_status(item["text"].strip()) for item in sources
                         if _active(item) and (item is source or _inside(item, source))} - {None}
        if len(active_stages) > 1:
            return None, "status_evidence_conflict"
    if (not current and not literal_asserted) or not (asserted or baseline or marker or badge):
        return None, "record_present_status_unknown"
    if timeline_without_current({"context": quotation}) and not (asserted or marker or badge):
        return None, "record_present_status_unknown"
    if status_is_unasserted(label, status):
        return None, "status_semantics_unsupported"
    # A model may shorten a quotation just before a negative suffix. Locate it
    # in the full source, so "面试" cannot hide the immediately following
    # "未通过" (or "interview not scheduled") outside its chosen excerpt.
    for quote_start, _ in evidence_spans(source["text"], quotation):
        label_end = quote_start + quotation.rfind(label) + len(label)
        tail = re.match(r"\s*(?:尚未|暂未|仍未|还未|未通过|未安排|未开始|未进行|未获得|未发放|未收到|"
                        r"无需|取消|not\b|never\b)[^\n，。；;,.!?！？]{0,60}", source["text"][label_end:], re.I)
        if tail:
            full_label = label + tail.group(0)
            if status_is_unasserted(full_label, status) or explicit_label_status(full_label) not in {None, status}:
                return None, "status_semantics_unsupported"
    dom_labels = current_status_labels(matches[0]) if matches else []
    generic_dom = False
    if visual_current and matches and dom_labels:
        raw_labels = matches[0].get("raw_status_labels")
        all_dom_labels = [*dom_labels, matches[0].get("label"), matches[0].get("current_step_label"),
                          *(raw_labels if isinstance(raw_labels, list) else [])]
        all_dom_labels = [str(value).strip() for value in all_dom_labels if value]
        # A generic parser label asserts no stage; an independently scoped
        # active visual node may establish one. Do not suppress a real dated
        # submission, different canonical assertion, or ambiguous DOM labels.
        generic_dom = (bool(all_dom_labels)
                       and all(evidence_text_key(value) in {"流程中", "进行中"} for value in all_dom_labels)
                       and literal_record_status(str(matches[0].get("context") or matches[0].get("evidence") or "")) is None)
    # A scoped, audited visual current node can supersede dated submission
    # metadata, but never a different actual DOM current assertion. Canonical
    # agreement tolerates wording only; title/card/quote ownership stays exact.
    dated_history = visual_current and dom_labels and all(dated_submission_label(value) for value in dom_labels)
    current_dom_labels = [value for value in dom_labels if not dated_submission_label(value)]
    same_stage = bool(current_dom_labels) and {explicit_label_status(value) for value in current_dom_labels} == {status}
    if matches and not timeline_without_current(matches[0]) and dom_labels and evidence_text_key(label) not in {
        evidence_text_key(value) for value in dom_labels
    } and not generic_dom and not dated_history and not same_stage and not (
        badge and {explicit_label_status(value) for value in dom_labels} == {status}
    ):
        return None, "status_evidence_conflict"
    card = {**identity, "title": card_title,
            "raw_title": identity.get("raw_title") or identity.get("title") or card_title,
            "context": quotation, "evidence": quotation,
            "label": label, "status": status, "current_step_label": label,
            "signals": {"current_step_identified": True, "has_active_step": bool(marker)}}
    return card, None
