from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

from app.jobs.cancellation import JobCancelled
from app.schemas import (
    InvestigationAssistantTurn,
    ProjectBatchReviewResult,
    ProjectFileReviewPlan,
    ProjectInterpretationResult,
)


class LMStudioClient:
    def __init__(self, *, base_url: str, timeout: int, api_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_token = api_token

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def list_models(self) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", [])

    def _post_chat_completion(self, payload: dict[str, Any], *, allow_plain_fallback: bool = False) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if allow_plain_fallback and response.status_code == 400 and "response_format" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=fallback_payload,
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()

    def _stream_chat_completion(
        self,
        payload: dict[str, Any],
        *,
        allow_plain_fallback: bool = False,
        chunk_callback: Callable[[str], None] | None = None,
    ) -> str:
        stream_payload = dict(payload)
        stream_payload["stream"] = True

        def consume(active_payload: dict[str, Any]) -> str:
            fragments: list[str] = []
            with httpx.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=active_payload,
                timeout=self.timeout,
            ) as response:
                response.raise_for_status()
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    payload_chunk = json.loads(data)
                    delta = payload_chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    if isinstance(delta, list):
                        text = "".join(part.get("text", "") for part in delta if isinstance(part, dict))
                    else:
                        text = delta or ""
                    if text:
                        fragments.append(text)
                        if chunk_callback:
                            chunk_callback(text)
            return "".join(fragments)

        try:
            return consume(stream_payload)
        except httpx.HTTPStatusError:
            if allow_plain_fallback and "response_format" in stream_payload:
                fallback_payload = dict(stream_payload)
                fallback_payload.pop("response_format", None)
                return consume(fallback_payload)
            raise

    @staticmethod
    def _extract_message_content(payload: dict[str, Any]) -> str:
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
        return json.dumps(content)

    @staticmethod
    def _strip_reasoning_blocks(content: str) -> str:
        text = content.strip()
        while text.startswith("<think>") and "</think>" in text:
            _, _, remainder = text.partition("</think>")
            text = remainder.strip()
        return text

    @staticmethod
    def _extract_json_segment(text: str) -> str:
        start = min((index for index in (text.find("{"), text.find("[")) if index != -1), default=-1)
        if start == -1:
            raise ValueError("No JSON object found in LM Studio response.")

        opening = text[start]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]

        raise ValueError("Incomplete JSON object found in LM Studio response.")

    @staticmethod
    def _extract_json_content(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        text = LMStudioClient._strip_reasoning_blocks(text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = json.loads(LMStudioClient._extract_json_segment(text))
        if not isinstance(payload, dict):
            raise ValueError("LM Studio did not return a JSON object.")
        return payload

    @staticmethod
    def _normalize_batch_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)

        batch_summary = normalized.get("batch_summary")
        if batch_summary is None:
            normalized["batch_summary"] = ""
        elif not isinstance(batch_summary, str):
            normalized["batch_summary"] = str(batch_summary)

        notable_points = normalized.get("notable_points")
        if isinstance(notable_points, str):
            normalized["notable_points"] = [notable_points]
        elif isinstance(notable_points, list):
            normalized["notable_points"] = [str(item) for item in notable_points if str(item).strip()]
        else:
            normalized["notable_points"] = []

        return normalized

    @staticmethod
    def _extract_batch_review_from_text(content: str) -> dict[str, Any]:
        text = LMStudioClient._strip_reasoning_blocks(content.strip())
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        if not text:
            raise ValueError("LM Studio returned an empty batch review response.")

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        bullet_points: list[str] = []
        narrative_lines: list[str] = []

        for line in lines:
            cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
            if not cleaned:
                continue
            lower_cleaned = cleaned.lower()
            if lower_cleaned.startswith(("notable points:", "key points:", "points:")):
                remainder = cleaned.split(":", 1)[1].strip() if ":" in cleaned else ""
                if remainder:
                    bullet_points.append(remainder)
                continue
            if line[:1] in {"-", "*", "•"} or re.match(r"^\d+[.)]\s+", line):
                bullet_points.append(cleaned)
            else:
                narrative_lines.append(cleaned)

        summary = ""
        if narrative_lines:
            summary = narrative_lines[0]
            if summary.lower().startswith(("summary:", "batch summary:")) and ":" in summary:
                summary = summary.split(":", 1)[1].strip()
        if not summary:
            first_sentence = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0].strip()
            summary = re.sub(r"^\s*(?:summary|batch summary)\s*:\s*", "", first_sentence, flags=re.IGNORECASE)

        if not bullet_points:
            remaining_text = " ".join(narrative_lines[1:] if narrative_lines else [])
            sentences = [
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", remaining_text)
                if sentence.strip()
            ]
            bullet_points = sentences[:6]

        return {
            "batch_summary": summary[:1200],
            "notable_points": bullet_points[:6],
        }

    @staticmethod
    def _normalize_interpretation_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)

        def normalize_text_field(field_name: str, fallback: str | None = None) -> None:
            value = normalized.get(field_name, fallback)
            if value is None:
                normalized[field_name] = ""
            elif not isinstance(value, str):
                normalized[field_name] = str(value)
            else:
                normalized[field_name] = value

        legacy_title = normalized.get("title")
        legacy_short_summary = normalized.get("short_summary")
        legacy_detailed_summary = normalized.get("detailed_summary")
        legacy_category = normalized.get("suggested_category")

        normalize_text_field("canonical_name", legacy_title)
        normalize_text_field("one_line_summary", legacy_short_summary)
        normalize_text_field("operator_summary", legacy_detailed_summary)
        normalize_text_field("likely_role", legacy_short_summary)

        primary_stack = normalized.get("primary_stack")
        if isinstance(primary_stack, list):
            normalized["primary_stack"] = ", ".join(str(item) for item in primary_stack if str(item).strip())
        elif primary_stack is None:
            normalized["primary_stack"] = ""
        elif not isinstance(primary_stack, str):
            normalized["primary_stack"] = str(primary_stack)

        tags = normalized.get("tags")
        if isinstance(tags, str):
            normalized["tags"] = [tags]
        elif isinstance(tags, list):
            normalized["tags"] = [str(item) for item in tags if str(item).strip()]

        evidence = normalized.get("evidence")
        if isinstance(evidence, str):
            normalized["evidence"] = [evidence]
        elif isinstance(evidence, list):
            normalized["evidence"] = [str(item) for item in evidence if str(item).strip()]

        unknowns = normalized.get("unknowns")
        if isinstance(unknowns, str):
            normalized["unknowns"] = [unknowns]
        elif isinstance(unknowns, list):
            normalized["unknowns"] = [str(item) for item in unknowns if str(item).strip()]

        risks = normalized.get("risks_or_notes")
        if isinstance(risks, str):
            normalized["risks_or_notes"] = [risks]
        elif isinstance(risks, list):
            normalized["risks_or_notes"] = [str(item) for item in risks if str(item).strip()]

        asset_kind = normalized.get("asset_kind")
        if not isinstance(asset_kind, str) or not asset_kind.strip():
            asset_kind = legacy_category
        asset_kind_text = str(asset_kind or "").strip().lower().replace(" ", "-")
        asset_kind_map = {
            "host": "host",
            "hypervisor": "hypervisor",
            "vm": "vm",
            "virtual-machine": "vm",
            "service": "service",
            "application": "application",
            "app": "application",
            "repository": "repository",
            "repo": "repository",
            "config-set": "config-set",
            "config": "config-set",
            "configuration": "config-set",
            "unknown": "unknown",
            "virtualization": "hypervisor",
            "web-app": "application",
            "networking": "service",
        }
        normalized["asset_kind"] = asset_kind_map.get(asset_kind_text, "unknown")

        confidence = normalized.get("confidence")
        if isinstance(confidence, str):
            try:
                normalized["confidence"] = float(confidence)
            except ValueError:
                lowered = confidence.strip().lower()
                confidence_map = {
                    "very low": 0.15,
                    "low": 0.35,
                    "medium": 0.65,
                    "moderate": 0.65,
                    "high": 0.85,
                    "very high": 0.95,
                }
                if lowered in confidence_map:
                    normalized["confidence"] = confidence_map[lowered]
        elif isinstance(confidence, int):
            normalized["confidence"] = float(confidence)

        normalized.setdefault("tags", [])
        normalized.setdefault("evidence", [])
        normalized.setdefault("unknowns", [])
        normalized.setdefault("risks_or_notes", [])
        if not normalized.get("primary_stack"):
            normalized["primary_stack"] = "Unknown"
        if not isinstance(normalized.get("confidence"), float):
            normalized["confidence"] = 0.5
        if not normalized.get("canonical_name"):
            normalized["canonical_name"] = "Unknown asset"
        if not normalized.get("one_line_summary"):
            normalized["one_line_summary"] = "Human verification recommended; evidence was limited."
        if not normalized.get("operator_summary"):
            normalized["operator_summary"] = normalized["one_line_summary"]
        if not normalized.get("likely_role"):
            normalized["likely_role"] = "Likely role is unclear from the available evidence."

        return normalized

    @staticmethod
    def _normalize_investigation_turn_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)

        def normalize_text(field_name: str) -> None:
            value = normalized.get(field_name)
            if value is None:
                normalized[field_name] = None
            elif isinstance(value, str):
                normalized[field_name] = value.strip() or None
            else:
                normalized[field_name] = str(value).strip() or None

        normalize_text("assistant_message")
        normalize_text("draft_title")
        normalize_text("draft_overview")
        normalize_text("draft_report_text")
        normalize_text("publish_title")
        normalize_text("publish_summary")
        normalize_text("publish_long_description")
        normalize_text("role_in_lab")

        for list_key in ("detail_sections", "setup_summary", "notes_or_highlights"):
            value = normalized.get(list_key)
            if isinstance(value, list):
                normalized[list_key] = [str(item).strip() for item in value if str(item).strip()] or None
            elif value is not None:
                normalized[list_key] = None

        custom = normalized.get("custom_sections")
        if isinstance(custom, list):
            cleaned = []
            for sec in custom:
                if isinstance(sec, dict) and sec.get("heading"):
                    items = [str(i).strip() for i in (sec.get("items") or []) if str(i).strip()]
                    if items:
                        cleaned.append({"heading": str(sec["heading"]).strip(), "items": items})
            normalized["custom_sections"] = cleaned or None
        elif custom is not None:
            normalized["custom_sections"] = None

        question = normalized.get("question")
        if isinstance(question, str):
            normalized["question"] = {
                "question_text": question.strip(),
                "question_type": None,
                "ai_rationale": None,
            }
        elif isinstance(question, dict):
            normalized["question"] = {
                "question_text": str(question.get("question_text") or "").strip(),
                "question_type": str(question.get("question_type")).strip() if question.get("question_type") else None,
                "ai_rationale": str(question.get("ai_rationale")).strip() if question.get("ai_rationale") else None,
            }
            if not normalized["question"]["question_text"]:
                normalized["question"] = None
        else:
            normalized["question"] = None

        normalized["ready_for_confirmation"] = bool(normalized.get("ready_for_confirmation"))
        return normalized

    @staticmethod
    def _extract_requested_paths_from_text(content: str, candidate_paths: list[str], max_files: int) -> list[str]:
        if not content.strip():
            return []

        normalized_candidates = {
            candidate.lower(): candidate
            for candidate in candidate_paths
            if candidate and isinstance(candidate, str)
        }
        matches: list[str] = []
        for lower_candidate, original_candidate in normalized_candidates.items():
            if lower_candidate in content.lower():
                matches.append(original_candidate)
        if matches:
            return matches[:max_files]

        token_candidates = re.findall(r"[/A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+", content)
        seen: set[str] = set()
        for token in token_candidates:
            token_lower = token.lower()
            for lower_candidate, original_candidate in normalized_candidates.items():
                if token_lower.endswith(lower_candidate) and original_candidate not in seen:
                    seen.add(original_candidate)
                    matches.append(original_candidate)
                    break
            if len(matches) >= max_files:
                break
        return matches[:max_files]

    def prompt_test(self, model_id: str) -> dict[str, Any]:
        payload = {
            "model": model_id,
            "temperature": 0,
            "max_tokens": 48,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with one short sentence confirming LM Studio can answer requests.",
                }
            ],
        }
        response_payload = self._post_chat_completion(payload)
        return {
            "content": self._extract_message_content(response_payload),
            "raw": response_payload,
        }

    def interpret_project(
        self,
        *,
        model_id: str,
        project_packet: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
        stream_callback: Callable[[str], None] | None = None,
    ) -> ProjectInterpretationResult:
        payload = {
            "model": model_id,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an internal infrastructure inventory assistant. "
                        "Write grounded, operator-facing summaries for an admin reviewing scanned inventory in a dashboard. "
                        "Use precise, low-hype wording and keep conclusions tied to observed evidence from the packet. "
                        "If batch_reviews are present, treat them as accumulated evidence from a broader file sweep and synthesize them carefully. "
                        "If operator_context is present, use it as owner guidance only when it does not conflict with the observed packet. "
                        "If the scanned item looks like infrastructure rather than a software project, describe it as infrastructure. "
                        "Be conservative with claims. If something is inferred rather than directly observed, label it as likely or probable. "
                        "Do not invent services, integrations, or purposes that are not supported by the evidence. "
                        "If the evidence is weak, lower confidence and say so. "
                        "Prefer exact file and path references when available. "
                        "Do not use marketing or portfolio language. Avoid phrases like 'powerful', 'robust', 'designed to', "
                        "'serves as the backbone', 'critical platform', 'supports a variety of', or 'advanced'. "
                        "Prefer phrases like 'Indicators suggest', 'Observed files include', 'This appears to be', "
                        "'Likely role', and 'Human verification recommended for'. "
                        "Distinguish clearly between observed facts, likely role, uncertainty, and risks/notes. "
                        "Respond with JSON that exactly matches the requested schema. "
                        "Do not include chain-of-thought, <think> blocks, or any prose before or after the JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Analyze this scanned item and return JSON with these keys: "
                        "asset_kind, canonical_name, one_line_summary, operator_summary, evidence, likely_role, "
                        "primary_stack, tags, confidence, unknowns, risks_or_notes.\n\n"
                        "Important guidance:\n"
                        "- Write like an internal infrastructure inventory assistant, not a sales or portfolio writer.\n"
                        "- Keep summaries useful for an admin reviewing inventory in a dashboard.\n"
                        "- Ground every major conclusion in observed evidence from the packet.\n"
                        "- If setup_kind is host-summary, treat the host itself as the asset and describe the contained systems in operational terms.\n"
                        "- If the scanned item looks like infrastructure rather than a software project, describe it as infrastructure.\n"
                        "- If batch_reviews are present, merge them together and avoid repeating the same point.\n"
                        "- Use review_coverage to understand how much of the safe file set was actually reviewed.\n"
                        "- Mention host sizing or platform details only when they are actually present.\n"
                        "- If this is a VM host or hypervisor, summarize the guest inventory or configuration instead of defining the hypervisor generically.\n"
                        "- If this is a service node, config set, or repository, say so directly.\n"
                        "- Do not invent services, integrations, or purposes that are not supported by the evidence.\n"
                        "- If evidence is weak, lower confidence and say so.\n"
                        "- asset_kind must be one of: host, hypervisor, vm, service, application, repository, config-set, unknown.\n"
                        "- canonical_name should be plain and descriptive, not embellished.\n"
                        "- one_line_summary should be a single concrete sentence.\n"
                        "- operator_summary should be 2-5 compact, grounded sentences.\n"
                        "- evidence should list specific observed paths, marker files, or metadata patterns.\n"
                        "- likely_role should describe what the system appears to do operationally.\n"
                        "- unknowns should explicitly list unresolved items.\n"
                        "- risks_or_notes should be practical and short.\n\n"
                        f"{json.dumps(project_packet, indent=2, sort_keys=True)}"
                    ),
                },
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream_callback:
            try:
                content = self._stream_chat_completion(
                    payload,
                    allow_plain_fallback=True,
                    chunk_callback=stream_callback,
                )
            except JobCancelled:
                raise
            except Exception:
                response_payload = self._post_chat_completion(payload, allow_plain_fallback=True)
                content = self._extract_message_content(response_payload)
                stream_callback(content)
        else:
            response_payload = self._post_chat_completion(payload, allow_plain_fallback=True)
            content = self._extract_message_content(response_payload)
        parsed_payload = self._normalize_interpretation_payload(self._extract_json_content(content))
        return ProjectInterpretationResult.model_validate(parsed_payload)

    def investigation_turn(
        self,
        *,
        model_id: str,
        session_packet: dict[str, Any],
        temperature: float,
        max_tokens: int = 1200,
    ) -> InvestigationAssistantTurn:
        payload = {
            "model": model_id,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the live investigation assistant inside AICMS. "
                        "This is an internal admin-only workspace for an operator investigating a host or project. "
                        "Hold a direct, useful conversation with the operator using the current findings, draft, proposals, and recent chat. "
                        "The local facts, topology, signature result, and current draft are authoritative. "
                        "If web_search_results are provided, use them to enrich the draft with real-world context (setup guides, best practices, etc). "
                        "Web search is triggered when the operator mentions 'websearch', 'search the web', 'look up', etc. in their message. "
                        "Use the operator's latest message to improve the draft and proposed wording. "
                        "The packet may include a rolling conversation_context_summary plus recent_messages; use both so the reply stays consistent with the ongoing chat. "
                        "You may ask at most one targeted follow-up question only if it would materially improve the investigation result. "
                        "Never repeat a previously asked question or question type unless the operator explicitly asked to revisit it. "
                        "Treat user instructions like remove, avoid, exclude, strip out, or focus as editorial constraints, not as public content to repeat. "
                        "Never echo those editorial instructions literally into the draft or the assistant reply. "
                        "Do not claim changes were applied. Do not invent facts outside the packet. "
                        "Return JSON with keys assistant_message, draft_title, draft_overview, draft_report_text, publish_title, publish_summary, publish_long_description, "
                        "detail_sections (list of strings for the build-details walkthrough), role_in_lab (string describing the machine's role), "
                        "setup_summary (list of strings describing how the machine is set up), notes_or_highlights (list of strings with notable observations), "
                        "custom_sections (list of {heading: string, items: list of strings} for any additional named sections you want to add or update — use this to create arbitrary sections like 'Security notes', 'Network topology', etc.; set to null if unchanged, or to an empty list to remove all custom sections), "
                        "ready_for_confirmation, question. "
                        "Set question to null if no question is needed. Set list fields to null if unchanged."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Use this investigation packet to produce the next assistant turn and revised draft. "
                        "The assistant_message should read like a direct reply to the operator, not a canned system notice. "
                        "The assistant_message is required; do not leave it blank. "
                        "Keep the draft aligned with the visible conversation and the rolling conversation summary. "
                        "If allowed_question_candidates is empty, question must be null. "
                        "If the operator has already answered enough, set ready_for_confirmation to true.\n\n"
                        f"{json.dumps(session_packet, indent=2, sort_keys=True)}"
                    ),
                },
            ],
        }
        response_payload = self._post_chat_completion(payload, allow_plain_fallback=True)
        content = self._extract_message_content(response_payload)
        parsed_payload = self._normalize_investigation_turn_payload(self._extract_json_content(content))
        return InvestigationAssistantTurn.model_validate(parsed_payload)

    def review_project_batch(
        self,
        *,
        model_id: str,
        project_packet: dict[str, Any],
        batch_index: int,
        batch_count: int,
    ) -> ProjectBatchReviewResult:
        payload = {
            "model": model_id,
            "temperature": 0,
            "max_tokens": 550,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You review a small batch of project files before a final merged summary is written. "
                        "Summarize what this batch specifically reveals about the host or setup. "
                        "Prefer concrete observed details about services, VM/container definitions, deployment shape, "
                        "resource sizing, ports, tunnels, operator intent, and configuration differences. "
                        "If operator_context is present, use it as owner guidance without inventing unsupported claims. "
                        "Respond with JSON using keys batch_summary and notable_points."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Review batch {batch_index} of {batch_count}. "
                        "Write a concise owner-facing summary of what this batch reveals, then list the most important concrete points.\n\n"
                        f"{json.dumps(project_packet, indent=2, sort_keys=True)}"
                    ),
                },
            ],
        }
        response_payload = self._post_chat_completion(payload, allow_plain_fallback=True)
        content = self._extract_message_content(response_payload)
        try:
            parsed_payload = self._normalize_batch_review_payload(self._extract_json_content(content))
        except Exception:
            parsed_payload = self._normalize_batch_review_payload(self._extract_batch_review_from_text(content))
        return ProjectBatchReviewResult.model_validate(parsed_payload)

    def plan_file_review(
        self,
        *,
        model_id: str,
        project_packet: dict[str, Any],
        max_files: int = 8,
    ) -> ProjectFileReviewPlan:
        payload = {
            "model": model_id,
            "temperature": 0,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You review host and project layouts before writing an owner-facing summary. "
                        "Select only a few safe text files worth inspecting next, prioritizing configuration, service, "
                        "inventory, VM definition, compose, README, or orchestration files that explain how the setup actually works. "
                        "Respond with JSON using keys requested_paths and notes."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Choose up to "
                        f"{max_files} file paths from the project packet that would most improve a project summary. "
                        "Only choose paths that already appear in the candidate list. "
                        "Prefer changed or never-reviewed files. "
                        "For infrastructure and host setups, favor the files that reveal deployment shape, VM/container configuration, services, "
                        "resource sizing, network exposure, and operator intent. "
                        "Return JSON with requested_paths as a list of strings and notes as a list of strings.\n\n"
                        f"{json.dumps(project_packet, indent=2, sort_keys=True)}"
                    ),
                },
            ],
        }
        response_payload = self._post_chat_completion(payload, allow_plain_fallback=True)
        content = self._extract_message_content(response_payload)
        try:
            return ProjectFileReviewPlan.model_validate(self._extract_json_content(content))
        except Exception:
            candidate_paths = [
                item.get("relative_path")
                for item in project_packet.get("file_inventory", [])
                if isinstance(item, dict) and item.get("relative_path")
            ]
            requested_paths = self._extract_requested_paths_from_text(content, candidate_paths, max_files)
            if requested_paths:
                return ProjectFileReviewPlan(
                    requested_paths=requested_paths,
                    notes=["Recovered requested paths from non-JSON LM Studio output."],
                )
            raise
