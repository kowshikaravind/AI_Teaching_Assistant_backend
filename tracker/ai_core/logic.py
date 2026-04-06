import logging
import os
import json
import re
import time
from collections import Counter, defaultdict

from django.conf import settings
from django.core.cache import cache
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
logger = logging.getLogger(__name__)

_LLM_COOLDOWN_KEY = "apt:ai:llm:cooldown:until"


def _is_llm_enabled():
    return bool(getattr(settings, "APT_AI_LLM_ENABLED", True))


def _llm_model_name():
    return str(getattr(settings, "APT_AI_LLM_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash")


def _llm_cooldown_seconds():
    try:
        return max(10, int(getattr(settings, "APT_AI_LLM_COOLDOWN_SECONDS", 180) or 180))
    except Exception:
        return 180


def _llm_max_calls_per_minute():
    try:
        return max(1, int(getattr(settings, "APT_AI_LLM_MAX_CALLS_PER_MINUTE", 6) or 6))
    except Exception:
        return 6


def _llm_max_retries():
    try:
        return max(0, int(getattr(settings, "APT_AI_LLM_MAX_RETRIES", 1) or 1))
    except Exception:
        return 1


def _llm_backoff_seconds():
    try:
        return max(0.0, float(getattr(settings, "APT_AI_LLM_BACKOFF_SECONDS", 1.0) or 1.0))
    except Exception:
        return 1.0


def _in_llm_cooldown():
    until = cache.get(_LLM_COOLDOWN_KEY)
    if until is None:
        return False
    try:
        return float(until) > time.time()
    except Exception:
        return False


def _start_llm_cooldown(seconds):
    ttl = max(10, int(seconds or _llm_cooldown_seconds()))
    cache.set(_LLM_COOLDOWN_KEY, time.time() + ttl, timeout=ttl)


def _reserve_llm_call_slot():
    max_calls = _llm_max_calls_per_minute()
    bucket = int(time.time() // 60)
    key = f"apt:ai:llm:calls:{bucket}"
    current = cache.get(key)
    if current is None:
        cache.set(key, 1, timeout=70)
        return True
    try:
        count = int(current)
    except Exception:
        count = 0
    if count >= max_calls:
        return False
    cache.set(key, count + 1, timeout=70)
    return True


def _retry_delay_from_error(message):
    text = _normalize_text(message)
    match = re.search(r"retry\s+in\s+([0-9]*\.?[0-9]+)s", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return max(1, int(float(match.group(1))))
    except Exception:
        return None


def _is_quota_error(message):
    text = _normalize_text(message).lower()
    return "429" in text or "quota" in text or "rate limit" in text


def _extract_json_object(text):
    raw = _normalize_text(text)
    if not raw:
        return None

    # First try direct JSON.
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    # Then try fenced block.
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue

    # Last attempt: find outer-most JSON object.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw[start : end + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _option_text(options, answer_key):
    if not isinstance(options, dict):
        return ""
    key = _normalize_text(answer_key)
    if not key:
        return ""
    value = options.get(key)
    return _normalize_text(value)


def _build_llm_topic_payload(wrong_rows):
    grouped = defaultdict(list)
    for row in wrong_rows:
        grouped[row["topic"]].append(row)

    topics = []
    for topic, rows in grouped.items():
        topic_rows = []
        for row in rows:
            options = row.get("options") if isinstance(row.get("options"), dict) else {}
            topic_rows.append({
                "question_id": row.get("question_id"),
                "subtopic": row.get("subtopic"),
                "difficulty": row.get("difficulty"),
                "question_text": row.get("question_text"),
                "options": options,
                "student_answer_key": row.get("student_answer"),
                "student_answer_text": _option_text(options, row.get("student_answer")),
                "correct_answer_key": row.get("correct_answer"),
                "correct_answer_text": _option_text(options, row.get("correct_answer")),
                "time_taken_seconds": row.get("time_taken_seconds", 0),
                "answer_changed": bool(row.get("answer_changed", False)),
            })
        topics.append({
            "topic": topic,
            "question_count": len(topic_rows),
            "mistakes": topic_rows,
        })
    return topics


def _run_llm_semantic_analysis(
    wrong_rows,
    student_name=None,
    test_name=None,
    subject_name=None,
):
    api_key = os.getenv("GOOGLE_API_KEY")
    if not _is_llm_enabled() or not api_key or not wrong_rows:
        return None

    if _in_llm_cooldown():
        logger.info("Skipping LLM semantic analysis due to cooldown window.")
        return None

    if not _reserve_llm_call_slot():
        logger.info("Skipping LLM semantic analysis due to per-minute call budget.")
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=_llm_model_name(),
            generation_config={"temperature": 0.2},
        )

        topics_payload = _build_llm_topic_payload(wrong_rows)
        prompt_payload = {
            "student_name": _normalize_text(student_name),
            "test_name": _normalize_text(test_name),
            "subject_name": _normalize_text(subject_name),
            "topics": topics_payload,
        }

        prompt = (
            "You are an expert academic reasoning analyst.\n"
            "Analyze only the provided wrong answers, grouped by topic.\n"
            "Infer conceptual misunderstandings and behavior patterns from question semantics, options chosen, and timing.\n"
            "Avoid generic statements. Explain WHY the student likely chose the wrong option.\n"
            "Connect patterns across multiple questions in the same topic and across topics when possible.\n"
            "Return ONLY valid JSON using this schema:\n"
            "{\n"
            "  \"topic_summaries\": [\n"
            "    {\n"
            "      \"topic\": \"string\",\n"
            "      \"understanding_summary\": \"2-4 sentence summary of conceptual understanding for this topic\",\n"
            "      \"key_misconceptions\": [\"...\"],\n"
            "      \"cross_question_pattern\": \"How mistakes connect across this topic\"\n"
            "    }\n"
            "  ],\n"
            "  \"conceptual_patterns\": [\"3-6 concise pattern statements with reasoning\"],\n"
            "  \"behavior_patterns\": [\"2-5 behavior interpretations tied to evidence\"],\n"
            "  \"detailed_mistakes\": [\n"
            "    {\n"
            "      \"question_id\": \"string or number\",\n"
            "      \"classification\": \"Conceptual Error|Careless Mistake|Misinterpretation|Logic Error|Guessing\",\n"
            "      \"why_student_chose_this\": \"reasoned explanation\",\n"
            "      \"why_it_is_wrong\": \"specific correction\",\n"
            "      \"correct_thinking\": \"how to think to get it right next time\",\n"
            "      \"memory_tip\": \"short tip\"\n"
            "    }\n"
            "  ],\n"
            "  \"overall_understanding_summary\": \"overall topic-level understanding summary\",\n"
            "  \"improvement_plan\": [\"3-6 prioritized actions\"],\n"
            "  \"recommendations\": [\"2-5 recommendations\"]\n"
            "}\n"
            "If evidence is insufficient for any field, still provide best-effort but avoid saying unknown.\n"
            f"Input JSON:\n{json.dumps(prompt_payload, ensure_ascii=True)}"
        )

        retries = _llm_max_retries()
        for attempt in range(retries + 1):
            try:
                response = model.generate_content(prompt)
                parsed = _extract_json_object(getattr(response, "text", ""))
                if not isinstance(parsed, dict):
                    return None
                return parsed
            except Exception as exc:
                message = str(exc)
                if _is_quota_error(message):
                    delay = _retry_delay_from_error(message) or _llm_cooldown_seconds()
                    _start_llm_cooldown(delay)
                    logger.warning("LLM quota/rate-limit hit. Cooldown started for %ss.", delay)
                    return None
                if attempt < retries:
                    wait_seconds = _llm_backoff_seconds() * (2 ** attempt)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    continue
                raise
    except Exception as exc:
        logger.warning("LLM semantic analysis failed: %s", exc)
        return None


def _normalize_text(value):
    return str(value or "").strip()


def _format_subject_insights(subject_insights):
    if not subject_insights:
        return ""

    if isinstance(subject_insights, dict):
        preferred_order = [
            "avg_score",
            "recent_score",
            "strengths",
            "weak_topics",
            "critical_weak_areas",
            "conceptual_mistakes",
            "behavior_patterns",
            "mastery_summary",
            "personalized_feedback",
            "improvement_plan",
            "practice_questions",
            "comprehensive_analysis",
        ]
        lines = []
        handled = set()

        def _append_line(label, value):
            cleaned = _normalize_text(value)
            if cleaned:
                lines.append(f"{label}: {cleaned}")

        for key in preferred_order:
            if key not in subject_insights:
                continue
            value = subject_insights.get(key)
            handled.add(key)
            if isinstance(value, list):
                flattened = ", ".join(_normalize_text(item) for item in value if _normalize_text(item))
                _append_line(key.replace("_", " ").title(), flattened)
            elif isinstance(value, dict):
                flattened = ", ".join(
                    f"{_normalize_text(item_key)}={_normalize_text(item_value)}"
                    for item_key, item_value in value.items()
                    if _normalize_text(item_key) and _normalize_text(item_value)
                )
                _append_line(key.replace("_", " ").title(), flattened)
            else:
                _append_line(key.replace("_", " ").title(), value)

        for key, value in subject_insights.items():
            if key in handled:
                continue
            if isinstance(value, list):
                flattened = ", ".join(_normalize_text(item) for item in value if _normalize_text(item))
                _append_line(str(key).replace("_", " ").title(), flattened)
            elif isinstance(value, dict):
                flattened = ", ".join(
                    f"{_normalize_text(item_key)}={_normalize_text(item_value)}"
                    for item_key, item_value in value.items()
                    if _normalize_text(item_key) and _normalize_text(item_value)
                )
                _append_line(str(key).replace("_", " ").title(), flattened)
            else:
                _append_line(str(key).replace("_", " ").title(), value)

        return "\n".join(lines)

    if isinstance(subject_insights, list):
        return "\n".join(f"- {_normalize_text(item)}" for item in subject_insights if _normalize_text(item))

    return _normalize_text(subject_insights)


def _contains_negation(question_text):
    text = f" {_normalize_text(question_text).lower()} "
    markers = (
        " not ",
        " except ",
        " least ",
        " false ",
        " incorrect ",
        " never ",
        " cannot ",
        "n't ",
    )
    return any(marker in text for marker in markers)


def _normalize_review_row(row, index):
    if not isinstance(row, dict):
        return None

    question_id = row.get("question_id", row.get("id", index))
    question_text = row.get("question_text") or row.get("question") or row.get("question_label") or ""
    student_answer = row.get("student_answer", row.get("selected_answer", row.get("answer", "")))
    correct_answer = row.get("correct_answer", row.get("expected_answer", ""))
    topic = row.get("topic", row.get("subject", "General"))
    subtopic = row.get("subtopic", row.get("sub_topic", row.get("topic_detail", "")))

    try:
        time_taken_seconds = max(0, int(row.get("time_taken_seconds", row.get("response_time", 0)) or 0))
    except (TypeError, ValueError):
        time_taken_seconds = 0

    return {
        "question_id": question_id,
        "question_text": _normalize_text(question_text),
        "student_answer": _normalize_text(student_answer),
        "correct_answer": _normalize_text(correct_answer),
        "topic": _normalize_text(topic) or "General",
        "subtopic": _normalize_text(subtopic),
        "time_taken_seconds": time_taken_seconds,
        "answer_changed": bool(row.get("answer_changed", False)),
        "difficulty": _normalize_text(row.get("difficulty", row.get("level", "Medium"))) or "Medium",
        "options": row.get("options", {}),
    }


def _topic_display(topic, subtopic):
    topic = _normalize_text(topic) or "General"
    subtopic = _normalize_text(subtopic)
    return f"{topic} - {subtopic}" if subtopic else topic


def _classify_mistake(row, topic_stats):
    topic = row["topic"]
    topic_accuracy = topic_stats.get(topic, {}).get("accuracy", 0)
    student_answer = row["student_answer"]
    time_taken_seconds = int(row.get("time_taken_seconds", 0) or 0)
    question_text = row["question_text"]

    if not student_answer:
        return (
            "Guessing",
            "No answer was provided, so there is no reasoning to check.",
        )

    if _contains_negation(question_text):
        return (
            "Misinterpretation",
            "The question uses negation or exception wording, which is easy to read incorrectly.",
        )

    if row.get("answer_changed") and time_taken_seconds and time_taken_seconds < 20:
        return (
            "Careless Mistake",
            "The answer was changed or rushed, which points to a quick final choice instead of careful checking.",
        )

    if time_taken_seconds >= 90:
        return (
            "Logic Error",
            "The question took a long time, but the final reasoning still led to the wrong answer.",
        )

    if topic_accuracy <= 50:
        return (
            "Conceptual Error",
            f"This topic is still weak overall ({topic_accuracy}% correct), so the core idea likely needs revision.",
        )

    if row.get("difficulty", "").lower() == "hard" and topic_accuracy < 75:
        return (
            "Logic Error",
            "The topic is partly understood, but the harder question exposed a reasoning gap.",
        )

    return (
        "Careless Mistake",
        "The concept seems partly known, but the final choice was not checked carefully enough.",
    )


def _memory_tip(label):
    tips = {
        "Conceptual Error": "Re-learn the definition first, then solve one easy example.",
        "Careless Mistake": "Slow down, read the final option again, and re-check before submitting.",
        "Misinterpretation": "Circle words like not, except, least, and false before choosing an answer.",
        "Logic Error": "Write the steps in order before selecting the answer.",
        "Guessing": "Eliminate impossible options first, then make the safest choice.",
    }
    return tips.get(label, "Review the concept, then try one similar question.")


def _practice_question(topic, subtopic, difficulty="Easy"):
    focus = _topic_display(topic, subtopic)
    if difficulty == "Medium":
        question = f"Apply the main idea of {focus} to a new example."
    else:
        question = f"What is the core idea of {focus}?"

    return {
        "topic": _normalize_text(topic) or "General",
        "subtopic": _normalize_text(subtopic),
        "difficulty": difficulty,
        "question": question,
        "hint": f"Start with the definition of {focus} and connect it to one simple example.",
    }


def analyze_test_submission(question_rows, student_name=None, test_name=None, subject_name=None, use_llm=True):
    normalized_rows = []
    for index, row in enumerate(question_rows or [], start=1):
        normalized = _normalize_review_row(row, index)
        if normalized:
            normalized_rows.append(normalized)

    total_questions = len(normalized_rows)
    correct_rows = [row for row in normalized_rows if row["student_answer"] and row["student_answer"] == row["correct_answer"]]
    wrong_rows = [row for row in normalized_rows if not row["student_answer"] or row["student_answer"] != row["correct_answer"]]

    topic_stats = defaultdict(lambda: {
        "total": 0,
        "correct": 0,
        "incorrect": 0,
        "subtopics": Counter(),
        "wrong_rows": [],
    })

    for row in normalized_rows:
        topic = row["topic"]
        stats = topic_stats[topic]
        stats["total"] += 1
        if row["subtopic"]:
            stats["subtopics"][row["subtopic"]] += 1
        if row["student_answer"] and row["student_answer"] == row["correct_answer"]:
            stats["correct"] += 1
        else:
            stats["incorrect"] += 1
            stats["wrong_rows"].append(row)

    topic_summary = {}
    strong_topics = []
    weak_topics = []
    critical_weak_areas = []

    for topic, stats in topic_stats.items():
        total = int(stats["total"])
        correct = int(stats["correct"])
        incorrect = int(stats["incorrect"])
        accuracy = round((correct / total) * 100, 2) if total else 0
        dominant_subtopic = stats["subtopics"].most_common(1)[0][0] if stats["subtopics"] else ""
        topic_summary[topic] = {
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "accuracy": accuracy,
            "dominant_subtopic": dominant_subtopic,
        }

        entry = {
            "topic": topic,
            "subtopic": dominant_subtopic,
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "accuracy": accuracy,
        }
        if accuracy >= 80 and total >= 2:
            strong_topics.append(entry)
        if incorrect > 0:
            weak_topics.append(entry)
        if incorrect >= 2 or accuracy <= 50:
            critical_weak_areas.append(entry)

    strong_topics.sort(key=lambda item: (-item["accuracy"], -item["correct"], item["topic"]))
    weak_topics.sort(key=lambda item: (item["accuracy"], -item["incorrect"], item["topic"]))
    critical_weak_areas.sort(key=lambda item: (item["accuracy"], -item["incorrect"], item["topic"]))

    mistake_counter = Counter()
    behavior_counter = Counter()
    detailed_mistakes = []

    for row in wrong_rows:
        label, reason = _classify_mistake(row, topic_summary)
        mistake_counter[label] += 1
        if label in {"Careless Mistake", "Misinterpretation", "Guessing"}:
            behavior_counter[label] += 1
        else:
            behavior_counter["Concept/Reasoning"] += 1

        detailed_mistakes.append({
            "question_id": row["question_id"],
            "question": row["question_text"],
            "student_answer": row["student_answer"] or "No answer provided",
            "correct_answer": row["correct_answer"],
            "topic": row["topic"],
            "subtopic": row["subtopic"],
            "classification": label,
            "what_student_did_wrong": f"The response for {_topic_display(row['topic'], row['subtopic'])} did not match the correct answer.",
            "why_it_is_wrong": reason,
            "correct_concept": f"The correct answer is {row['correct_answer']}. Revisit the main rule or definition for {_topic_display(row['topic'], row['subtopic'])}.",
            "simple_explanation": f"Think of {_topic_display(row['topic'], row['subtopic'])} as the key idea you need to recognize before answering.",
            "memory_tip": _memory_tip(label),
        })

    repeated_weaknesses = []
    for topic, stats in topic_summary.items():
        if stats["incorrect"] >= 2:
            repeated_weaknesses.append({
                "type": "Topic",
                "label": topic,
                "count": stats["incorrect"],
                "message": f"{topic} is a repeated weak area with {stats['incorrect']} wrong answer(s).",
                "comparison": f"Accuracy in {topic} is only {stats['accuracy']}%, so the same concept is being missed again.",
                "extra_attention_note": f"Spend extra time on {topic} before moving to the next topic.",
            })

    for label, count in mistake_counter.items():
        if count >= 2:
            repeated_weaknesses.append({
                "type": "Mistake Pattern",
                "label": label,
                "count": count,
                "message": f"{label} happened {count} times.",
                "comparison": "This shows the same thinking pattern is repeating across more than one question.",
                "extra_attention_note": _memory_tip(label),
            })

    repeated_weakness = repeated_weaknesses[0] if repeated_weaknesses else None

    total_attempted = sum(1 for row in normalized_rows if row["student_answer"])
    accuracy = round((len(correct_rows) / total_questions) * 100, 2) if total_questions else 0

    performance_summary = {
        "student_name": _normalize_text(student_name),
        "test_name": _normalize_text(test_name),
        "subject_name": _normalize_text(subject_name),
        "total_questions": total_questions,
        "correct": len(correct_rows),
        "incorrect": len(wrong_rows),
        "attempted": total_attempted,
        "unattempted": max(0, total_questions - total_attempted),
        "accuracy": accuracy,
    }

    concept_patterns = []
    for entry in critical_weak_areas[:5]:
        concept_patterns.append(
            f"{entry['topic']} needs revision ({entry['correct']}/{entry['total']} correct)."
        )
    if not concept_patterns and strong_topics:
        concept_patterns.append(f"{strong_topics[0]['topic']} is a strength with {strong_topics[0]['accuracy']}% accuracy.")

    behavior_patterns = []
    if mistake_counter["Guessing"]:
        behavior_patterns.append(f"Guessing detected on {mistake_counter['Guessing']} question(s).")
    if mistake_counter["Careless Mistake"]:
        behavior_patterns.append(f"Rushing or weak checking affected {mistake_counter['Careless Mistake']} question(s).")
    if mistake_counter["Misinterpretation"]:
        behavior_patterns.append(f"Question wording was misread on {mistake_counter['Misinterpretation']} question(s).")
    if mistake_counter["Logic Error"]:
        behavior_patterns.append(f"Reasoning broke down on {mistake_counter['Logic Error']} question(s).")
    if not behavior_patterns:
        behavior_patterns.append("No repeated behavior pattern was detected.")

    llm_topic_summaries = []
    llm_result = None
    if use_llm and wrong_rows:
        llm_result = _run_llm_semantic_analysis(
            wrong_rows,
            student_name=student_name,
            test_name=test_name,
            subject_name=subject_name,
        )
        if isinstance(llm_result, dict):
            llm_concepts = [
                _normalize_text(item)
                for item in llm_result.get("conceptual_patterns", [])
                if _normalize_text(item)
            ]
            if llm_concepts:
                concept_patterns = llm_concepts

            llm_behaviors = [
                _normalize_text(item)
                for item in llm_result.get("behavior_patterns", [])
                if _normalize_text(item)
            ]
            if llm_behaviors:
                behavior_patterns = llm_behaviors

            topic_summaries = llm_result.get("topic_summaries", [])
            if isinstance(topic_summaries, list):
                for summary in topic_summaries:
                    if not isinstance(summary, dict):
                        continue
                    topic_name = _normalize_text(summary.get("topic"))
                    understanding = _normalize_text(summary.get("understanding_summary"))
                    cross_pattern = _normalize_text(summary.get("cross_question_pattern"))
                    misconceptions = [
                        _normalize_text(item)
                        for item in summary.get("key_misconceptions", [])
                        if _normalize_text(item)
                    ]
                    if topic_name or understanding or cross_pattern or misconceptions:
                        llm_topic_summaries.append({
                            "topic": topic_name,
                            "understanding_summary": understanding,
                            "cross_question_pattern": cross_pattern,
                            "key_misconceptions": misconceptions,
                        })

            detailed_items = llm_result.get("detailed_mistakes", [])
            llm_detail_map = {}
            if isinstance(detailed_items, list):
                for detail in detailed_items:
                    if not isinstance(detail, dict):
                        continue
                    qid = _normalize_text(detail.get("question_id"))
                    if not qid:
                        continue
                    llm_detail_map[qid] = detail

            if llm_detail_map:
                allowed_labels = {
                    "Conceptual Error",
                    "Careless Mistake",
                    "Misinterpretation",
                    "Logic Error",
                    "Guessing",
                }
                for item in detailed_mistakes:
                    qid = _normalize_text(item.get("question_id"))
                    llm_item = llm_detail_map.get(qid)
                    if not llm_item:
                        continue
                    llm_label = _normalize_text(llm_item.get("classification"))
                    if llm_label in allowed_labels:
                        item["classification"] = llm_label
                    why_student = _normalize_text(llm_item.get("why_student_chose_this"))
                    why_wrong = _normalize_text(llm_item.get("why_it_is_wrong"))
                    correct_thinking = _normalize_text(llm_item.get("correct_thinking"))
                    memory_tip = _normalize_text(llm_item.get("memory_tip"))
                    if why_student:
                        item["what_student_did_wrong"] = why_student
                    if why_wrong:
                        item["why_it_is_wrong"] = why_wrong
                    if correct_thinking:
                        item["correct_concept"] = correct_thinking
                        item["simple_explanation"] = correct_thinking
                    if memory_tip:
                        item["memory_tip"] = memory_tip

    strengths = []
    if strong_topics:
        for entry in strong_topics[:4]:
            strengths.append(
                f"{entry['topic']} looks strong with {entry['accuracy']}% accuracy."
            )
    elif accuracy >= 70:
        strengths.append("You are showing a solid overall grasp of the test content.")
    else:
        strengths.append("There are still a few stable areas, but they are not strong enough to highlight yet.")

    weaknesses = []
    for entry in critical_weak_areas[:5]:
        weaknesses.append(
            f"{entry['topic']} needs more practice because {entry['incorrect']} question(s) were missed."
        )
    if not weaknesses and weak_topics:
        weaknesses.append(f"{weak_topics[0]['topic']} is the main topic to review next.")

    recommendations = []
    if repeated_weakness:
        recommendations.append(repeated_weakness["extra_attention_note"])
    for entry in critical_weak_areas[:3]:
        recommendations.append(f"Revisit {entry['topic']} and solve 3 easy practice questions.")
    if not recommendations:
        recommendations.append("Keep practicing mixed questions to protect your current accuracy.")

    improvement_plan = []
    for entry in critical_weak_areas[:3]:
        improvement_plan.append(f"Revise {entry['topic']} using one short note and one solved example.")
    if mistake_counter["Misinterpretation"]:
        improvement_plan.append("Underline question words like not, except, least, and false before answering.")
    if mistake_counter["Careless Mistake"]:
        improvement_plan.append("Leave 10 seconds at the end to check your selected option again.")
    if mistake_counter["Guessing"]:
        improvement_plan.append("Eliminate obviously wrong options before choosing an answer.")
    if not improvement_plan:
        improvement_plan.append("Keep a steady revision routine and solve a short mixed quiz every day.")

    if isinstance(llm_result, dict):
        llm_plan = [
            _normalize_text(item)
            for item in llm_result.get("improvement_plan", [])
            if _normalize_text(item)
        ]
        llm_recommendations = [
            _normalize_text(item)
            for item in llm_result.get("recommendations", [])
            if _normalize_text(item)
        ]
        if llm_plan:
            improvement_plan = llm_plan
        if llm_recommendations:
            recommendations = llm_recommendations

    weak_topic_names = [entry["topic"] for entry in critical_weak_areas] or [entry["topic"] for entry in weak_topics]
    practice_questions = []
    seen_practice_topics = set()
    for entry in critical_weak_areas:
        if entry["topic"] in seen_practice_topics:
            continue
        seen_practice_topics.add(entry["topic"])
        practice_questions.append(_practice_question(entry["topic"], entry["subtopic"], "Easy"))
        if len(practice_questions) >= 5:
            break
    if len(practice_questions) < 2:
        for entry in strong_topics:
            if entry["topic"] in seen_practice_topics:
                continue
            practice_questions.append(_practice_question(entry["topic"], entry["subtopic"], "Medium"))
            seen_practice_topics.add(entry["topic"])
            if len(practice_questions) >= 2:
                break

    if not practice_questions:
        practice_questions = [
            _practice_question(subject_name or "General", "Core idea", "Easy"),
            _practice_question(subject_name or "General", "Application", "Easy"),
        ]

    mastery_summary = (
        f"{student_name or 'The student'} answered {len(correct_rows)} out of {total_questions} questions correctly "
        f"({accuracy}%)."
    )
    if critical_weak_areas:
        mastery_summary += f" The main revision focus is {critical_weak_areas[0]['topic']}."
    elif strong_topics:
        mastery_summary += f" The strongest area is {strong_topics[0]['topic']}."

    if isinstance(llm_result, dict):
        llm_summary = _normalize_text(llm_result.get("overall_understanding_summary"))
        if llm_summary:
            mastery_summary = llm_summary

    comprehensive_analysis = [
        f"Performance Summary: {len(correct_rows)}/{total_questions} correct ({accuracy}%).",
        f"Topic Analysis: {', '.join(entry['topic'] for entry in strong_topics[:3]) or 'No strong topic yet'}.",
        f"Weak Topics: {', '.join(entry['topic'] for entry in critical_weak_areas[:3]) or 'None detected'}.",
        f"Repeated Weakness: {repeated_weakness['label']}" if repeated_weakness else "Repeated Weakness: None detected.",
    ]

    for summary in llm_topic_summaries[:5]:
        topic = _normalize_text(summary.get("topic")) or "General"
        understanding = _normalize_text(summary.get("understanding_summary"))
        cross_pattern = _normalize_text(summary.get("cross_question_pattern"))
        misconceptions = summary.get("key_misconceptions", [])

        if understanding:
            comprehensive_analysis.append(f"{topic} Understanding: {understanding}")
        if cross_pattern:
            comprehensive_analysis.append(f"{topic} Pattern: {cross_pattern}")
        if misconceptions:
            comprehensive_analysis.append(
                f"{topic} Misconceptions: {', '.join(misconceptions[:3])}"
            )

    predicted_performance = {
        "risk_level": "high" if accuracy < 50 else "medium" if accuracy < 75 else "low",
        "focus_topics": weak_topic_names[:3],
        "expected_next_step": "Revise weak topics before attempting mixed practice.",
    }

    return {
        "performance_summary": performance_summary,
        "topic_analysis": {
            "by_topic": topic_summary,
            "strong_topics": strong_topics,
            "weak_topics": weak_topics,
            "critical_weak_areas": critical_weak_areas,
        },
        "mistake_breakdown": {
            "Conceptual Errors": mistake_counter["Conceptual Error"],
            "Careless Mistakes": mistake_counter["Careless Mistake"],
            "Misinterpretations": mistake_counter["Misinterpretation"],
            "Logic Errors": mistake_counter["Logic Error"],
            "Guessing": mistake_counter["Guessing"],
            "Correct": len(correct_rows),
            "Incorrect": len(wrong_rows),
        },
        "detailed_mistake_analysis": detailed_mistakes,
        "repeated_weakness": repeated_weakness,
        "personalized_feedback": " ".join([
            f"{student_name or 'The student'} completed {test_name or 'the test'} with {accuracy}% accuracy.",
            f"{mastery_summary}",
        ]).strip(),
        "improvement_plan": improvement_plan,
        "practice_questions": practice_questions,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "mastery_summary": mastery_summary,
        "conceptual_patterns": concept_patterns,
        "behavior_patterns": behavior_patterns,
        "comprehensive_analysis": comprehensive_analysis,
        "predicted_performance": predicted_performance,
    }


def validate_analysis_report(report):
    required_sections = [
        "performance_summary",
        "topic_analysis",
        "mistake_breakdown",
        "detailed_mistake_analysis",
        "personalized_feedback",
        "improvement_plan",
        "practice_questions",
    ]

    missing_sections = [section for section in required_sections if section not in report]
    detailed = report.get("detailed_mistake_analysis", []) if isinstance(report, dict) else []
    breakdown = report.get("mistake_breakdown", {}) if isinstance(report, dict) else {}

    issues = []
    if not isinstance(detailed, list):
        issues.append("detailed_mistake_analysis must be a list.")
    if not isinstance(breakdown, dict):
        issues.append("mistake_breakdown must be an object.")
    if isinstance(detailed, list):
        for idx, item in enumerate(detailed, start=1):
            if not isinstance(item, dict):
                issues.append(f"detailed_mistake_analysis[{idx}] must be an object.")
                continue
            for field in [
                "what_student_did_wrong",
                "why_it_is_wrong",
                "correct_concept",
                "simple_explanation",
                "memory_tip",
            ]:
                if field not in item:
                    issues.append(f"detailed_mistake_analysis[{idx}] is missing {field}.")

    score = max(0, 100 - (len(missing_sections) * 15) - (len(issues) * 5))
    return {
        "is_valid": not missing_sections and not issues,
        "coverage_score": score,
        "missing_sections": missing_sections,
        "issues": issues,
    }


def build_student_context(
    name,
    class_name,
    structured_marks,
    gender=None,
    parent_number=None,
    subject_focus=None,
    subject_insights=None,
):
    """Build a compact chat context for student/teacher AI chat only."""
    if structured_marks:
        marks_table = "Subject | Test Name | Date | Score | Percent\n"
        marks_table += "-" * 60 + "\n"
        for mark in structured_marks:
            marks_table += (
                f"{mark['subject']} | {mark['test_name']} | {mark['date']} | "
                f"{mark['marks_obtained']}/{mark['total_marks']} | {mark['percentage']}%\n"
            )
    else:
        marks_table = "No exam records available yet."

    subject_focus_text = f"\nCurrent Subject Focus: {subject_focus}" if subject_focus else ""
    formatted_subject_insights = _format_subject_insights(subject_insights)
    subject_insights_text = f"\n\nCurrent Subject Insights:\n{formatted_subject_insights}" if formatted_subject_insights else ""

    return (
        f"You are a student-facing Academic Tutor AI helping {name} improve based on real test evidence.\n\n"
        "Student profile:\n"
        f"Name: {name}\n"
        f"Class: {class_name}\n"
        f"Gender: {gender or 'N/A'}\n"
        f"Parent Contact: {parent_number or 'N/A'}{subject_focus_text}\n\n"
        "Exam history:\n"
        f"{marks_table}{subject_insights_text}\n\n"
        "Rules:\n"
        "- Stay on academic topics only\n"
        "- Use only the provided student data\n"
        "- Keep response concise, specific, and actionable\n"
        "- Avoid unrelated chat\n"
    )


def chat_with_student_context(student_context, conversation_history):
    """Run chat with Gemini using prepared context and conversation history."""
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return "Error: Google API Key not found."

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.35},
        )

        context_history = [
            {"role": "user", "parts": [student_context]},
            {
                "role": "model",
                "parts": [
                    "Understood. I will provide focused academic help using only the provided data."
                ],
            },
        ]

        for turn in conversation_history[:-1]:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            parts = turn.get("parts")
            if role not in {"user", "model"}:
                continue
            if not isinstance(parts, list) or not parts:
                continue
            context_history.append({"role": role, "parts": [str(parts[0])]})

        latest_message = ""
        if conversation_history and isinstance(conversation_history[-1], dict):
            latest_parts = conversation_history[-1].get("parts")
            if isinstance(latest_parts, list) and latest_parts:
                latest_message = str(latest_parts[0])

        chat = model.start_chat(history=context_history)
        response = chat.send_message(latest_message)
        return _normalize_text(getattr(response, "text", "")) or "I could not generate a response."
    except Exception as exc:
        logger.warning("Chat model call failed: %s", exc)
        return f"Error: {str(exc)}"


def fallback_student_chat_response(student_name, subject_name, subject_insights, latest_message):
    """Deterministic fallback if chat model is unavailable."""
    subject = _normalize_text(subject_name) or "this subject"
    insights = subject_insights if isinstance(subject_insights, dict) else {}

    avg_score = insights.get("avg_score")
    recent_score = insights.get("recent_score")

    lines = [f"Here is a focused review for {subject}, {student_name}:"]
    if avg_score is not None and recent_score is not None:
        lines.append(
            f"- Your recent trend in {subject} is {recent_score}% against an average of {avg_score}%."
        )
    elif recent_score is not None:
        lines.append(f"- Your latest score in {subject} is {recent_score}%.")

    normalized_message = _normalize_text(latest_message).lower()
    if any(word in normalized_message for word in ["next", "plan", "improve", "focus"]):
        lines.append("- Focus on revising one chapter at a time and solving mixed practice questions daily.")
    else:
        lines.append("- Ask about a specific chapter or mistake pattern for more targeted help.")

    return "\n".join(lines)
