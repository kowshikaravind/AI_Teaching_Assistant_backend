import os

from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

MIN_INCORRECT_FOR_CONCEPT = 2
MIN_QUESTIONS_FOR_BEHAVIOR = 3
MAX_INCORRECT_FOR_AI = 25

NO_REVIEW_MESSAGE = "No review available."
NO_CONCEPT_PATTERN_MESSAGE = "No strong patterns detected yet."
NO_BEHAVIOR_PATTERN_MESSAGE = "No clear behavior patterns detected."


def _normalize_difficulty(value):
    normalized = str(value or "medium").strip().lower()
    return normalized if normalized in {"easy", "medium", "hard"} else "medium"


def _normalize_topic(value):
    return str(value or "General").strip() or "General"


def _normalize_text(value):
    return str(value or "").strip()


def _clean_pattern_list(patterns, empty_message):
    cleaned = []
    seen = set()
    invalid = {
        NO_CONCEPT_PATTERN_MESSAGE.lower(),
        NO_BEHAVIOR_PATTERN_MESSAGE.lower(),
        NO_REVIEW_MESSAGE.lower(),
        "",
    }

    for pattern in patterns or []:
        line = _normalize_text(pattern)
        if line.startswith("*"):
            line = f"- {line[1:].strip()}"
        elif not line.startswith("-"):
            line = f"- {line.lstrip('- ').strip()}"

        key = line[1:].strip().lower() if line.startswith("-") else line.lower()
        if key in invalid:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(line)

    return cleaned if cleaned else [empty_message]


def _extract_bullets_only(text, max_items, empty_message):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    bullets = []
    for line in lines:
        if line.startswith("-"):
            bullets.append(line)
        elif line.startswith("*"):
            bullets.append(f"- {line[1:].strip()}")
    return _clean_pattern_list(bullets[:max_items], empty_message)


def _behavior_summary(questions_data):
    timed_questions = []
    for question in questions_data or []:
        try:
            time_taken = max(0, int(question.get("time_taken_seconds", 0) or 0))
        except Exception:
            time_taken = 0
        if time_taken > 0:
            timed_questions.append(time_taken)

    total_questions = len(questions_data or [])
    incorrect_count = sum(1 for question in questions_data or [] if not question.get("is_correct", False))
    changed_count = sum(1 for question in questions_data or [] if question.get("answer_changed"))

    if not timed_questions:
        return {
            "timed_questions": [],
            "avg_time": 0,
            "total_time": 0,
            "incorrect_count": incorrect_count,
            "changed_count": changed_count,
            "wrong_rate": 0,
            "compressed_attempt": False,
        }

    avg_time = sum(timed_questions) / len(timed_questions)
    wrong_rate = incorrect_count / total_questions if total_questions else 0
    compressed_attempt = (
        len(timed_questions) >= 5
        and avg_time <= 18
        and wrong_rate >= 0.6
        and changed_count <= 1
    )

    return {
        "timed_questions": timed_questions,
        "avg_time": avg_time,
        "total_time": sum(timed_questions),
        "incorrect_count": incorrect_count,
        "changed_count": changed_count,
        "wrong_rate": wrong_rate,
        "compressed_attempt": compressed_attempt,
    }


def _heuristic_conceptual_mistakes(questions_data):
    incorrect = [q for q in questions_data if not q.get("is_correct", False)]
    if len(incorrect) < MIN_INCORRECT_FOR_CONCEPT:
        return [NO_CONCEPT_PATTERN_MESSAGE]

    bullets = []
    same_answer_groups = {}
    for question in incorrect:
        selected = _normalize_text(question.get("selected_answer")).lower()
        correct = _normalize_text(question.get("correct_answer")).lower()
        topic = _normalize_topic(question.get("topic"))
        key = (selected, correct)
        same_answer_groups.setdefault(key, []).append(topic)

    repeated_choice_confusion = [
        (key, topics)
        for key, topics in same_answer_groups.items()
        if key[0] and key[1] and len(topics) >= 2
    ]
    if repeated_choice_confusion:
        (selected, correct), topics = repeated_choice_confusion[0]
        bullets.append(
            f"- In multiple questions, you are choosing '{selected}' when the logic points to '{correct}', which suggests you are applying the same wrong idea across similar situations."
        )

    changed_wrong = sum(1 for question in incorrect if question.get("answer_changed"))
    if changed_wrong >= 2:
        bullets.append(
            "- In several wrong answers, you seem to narrow down the options but then switch away from the correct line of thinking, which shows uncertainty in the underlying concept."
        )

    short_question_confusion = [
        question for question in incorrect
        if len(_normalize_text(question.get("question_text")).split()) <= 12
    ]
    if len(short_question_confusion) >= 2:
        bullets.append(
            "- Even in shorter direct questions, your answers suggest you are relying on familiar-looking options instead of checking what the question is actually asking."
        )

    difficulty_wrong = {"easy": 0, "medium": 0, "hard": 0}
    for question in incorrect:
        difficulty_wrong[_normalize_difficulty(question.get("difficulty"))] += 1

    if difficulty_wrong["easy"] >= 2:
        bullets.append(
            "- Some of these mistakes are happening on simpler questions too, which suggests the confusion is in the basic idea itself, not only in tougher applications."
        )

    return _clean_pattern_list(bullets[:4], NO_CONCEPT_PATTERN_MESSAGE)


def _heuristic_test_behavior(questions_data):
    if len(questions_data) < MIN_QUESTIONS_FOR_BEHAVIOR:
        return [NO_BEHAVIOR_PATTERN_MESSAGE]

    bullets = []

    def _tts(question):
        try:
            return max(0, int(question.get("time_taken_seconds", 0) or 0))
        except Exception:
            return 0

    timed_questions = [question for question in questions_data if _tts(question) > 0]
    if len(timed_questions) < MIN_QUESTIONS_FOR_BEHAVIOR:
        return [NO_BEHAVIOR_PATTERN_MESSAGE]

    summary = _behavior_summary(questions_data)
    total_time = summary["total_time"]
    avg_time = summary["avg_time"]
    if avg_time <= 0:
        return [NO_BEHAVIOR_PATTERN_MESSAGE]

    rush_threshold = avg_time * 0.5
    slow_threshold = avg_time * 1.6

    fast_wrong = [
        question for question in timed_questions
        if not question.get("is_correct", False) and _tts(question) < rush_threshold
    ]
    slow_wrong = [
        question for question in timed_questions
        if not question.get("is_correct", False) and _tts(question) > slow_threshold
    ]
    changed_count = sum(1 for question in timed_questions if question.get("answer_changed"))

    if len(fast_wrong) >= 2:
        bullets.append("- You are answering too quickly on several questions and getting them wrong.")

    if summary["compressed_attempt"]:
        bullets.append(
            "- You moved through most of the paper in one quick pass without slowing down to check your thinking, and that rushed approach is showing up in the wrong answers."
        )

    if len(slow_wrong) >= 2:
        bullets.append("- You are spending much longer on several questions without getting better results.")

    if changed_count >= max(2, len(timed_questions) // 3):
        bullets.append("- You are changing answers often, which suggests hesitation during the test.")

    by_difficulty = {"easy": [], "medium": [], "hard": []}
    for question in timed_questions:
        by_difficulty[_normalize_difficulty(question.get("difficulty"))].append(_tts(question))

    if by_difficulty["easy"] and by_difficulty["hard"]:
        easy_avg = sum(by_difficulty["easy"]) / len(by_difficulty["easy"])
        hard_avg = sum(by_difficulty["hard"]) / len(by_difficulty["hard"])
        if hard_avg < easy_avg * 0.85:
            bullets.append("- Your pacing across difficulty levels looks uneven, especially on harder questions.")

    return _clean_pattern_list(bullets[:4], NO_BEHAVIOR_PATTERN_MESSAGE)


def analyze_conceptual_mistakes(questions_data):
    """
    Detect repeated conceptual patterns from incorrect answers only.
    """
    if not questions_data:
        return [NO_REVIEW_MESSAGE]

    incorrect = [q for q in questions_data if not q.get("is_correct", False)]
    if len(incorrect) < MIN_INCORRECT_FOR_CONCEPT:
        return [NO_CONCEPT_PATTERN_MESSAGE]

    prepared = []
    for question in incorrect[:MAX_INCORRECT_FOR_AI]:
        prepared.append({
            "topic": _normalize_topic(question.get("topic")),
            "question_text": _normalize_text(question.get("question_text"))[:300],
            "selected_answer": _normalize_text(question.get("selected_answer")),
            "correct_answer": _normalize_text(question.get("correct_answer")),
            "difficulty": _normalize_difficulty(question.get("difficulty")),
            "answer_changed": bool(question.get("answer_changed")),
        })

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _heuristic_conceptual_mistakes(prepared)

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.2},
        )

        failures_text = ""
        for idx, question in enumerate(prepared, 1):
            failures_text += (
                f"\n{idx}. Topic: {question['topic']}\n"
                f"   Question: {question['question_text']}\n"
                f"   Student answer: {question['selected_answer'] or 'No answer'}\n"
                f"   Correct answer: {question['correct_answer'] or 'Not available'}\n"
                f"   Difficulty: {question['difficulty']}\n"
                f"   Answer changed: {'yes' if question['answer_changed'] else 'no'}\n"
            )

        prompt = f"""You are analyzing a student's incorrect answers from a real test.

Your goal is NOT to summarize mistakes.
Your goal is to deeply understand HOW the student is thinking wrong.

You must behave like a human teacher reviewing an answer sheet.

INPUT:
You will receive incorrect responses with:
- question_text
- selected_answer
- correct_answer
- topic

YOUR TASK:
1. Carefully read each incorrect question.
2. Understand what the student selected versus what is correct.
3. Identify the thinking error behind the mistake.

IMPORTANT:
- Do not just group by topic.
- Do not give generic patterns.
- Do not repeat template-style outputs.
- Do not mention scores, marks, percentages, statistics, or AI.
- Do not give filler advice.
- Only report a pattern if there is enough evidence across multiple incorrect answers.

You must:
- Identify what the student misunderstood conceptually.
- Explain how the student is thinking incorrectly.
- Detect confusion between similar concepts.
- Detect repeated use of wrong logic.
- Detect when the student is misreading or misinterpreting questions.

OUTPUT STYLE:
- Maximum 4 bullet points.
- Every bullet must start with "-".
- Each bullet must feel like a human teacher observation.
- Each bullet must explain a real reasoning mistake.
- Each bullet must connect multiple questions into one insight.
- Use natural, human, slightly conversational language.

EDGE CASE:
If there are not enough meaningful mistakes, return exactly:
{NO_CONCEPT_PATTERN_MESSAGE}

Incorrect answers:
{failures_text}
"""

        response = model.generate_content(prompt)
        result = getattr(response, "text", "").strip()
        parsed = _extract_bullets_only(result, 4, NO_CONCEPT_PATTERN_MESSAGE)
        return parsed if parsed != [NO_CONCEPT_PATTERN_MESSAGE] else _heuristic_conceptual_mistakes(prepared)
    except Exception:
        return _heuristic_conceptual_mistakes(prepared)


def analyze_test_behavior(questions_data):
    """
    Detect repeated behavior patterns from question timing and response actions.
    """
    if not questions_data:
        return [NO_REVIEW_MESSAGE]

    prepared = []
    for question in questions_data:
        prepared.append({
            "is_correct": bool(question.get("is_correct", False)),
            "time_taken_seconds": max(0, int(question.get("time_taken_seconds", 0) or 0)),
            "answer_changed": bool(question.get("answer_changed", False)),
            "difficulty": _normalize_difficulty(question.get("difficulty")),
            "topic": _normalize_topic(question.get("topic")),
        })

    if len(prepared) < MIN_QUESTIONS_FOR_BEHAVIOR:
        return [NO_BEHAVIOR_PATTERN_MESSAGE]

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _heuristic_test_behavior(prepared)

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.2},
        )

        timed = [q["time_taken_seconds"] for q in prepared if q["time_taken_seconds"] > 0]
        if len(timed) < MIN_QUESTIONS_FOR_BEHAVIOR:
            return [NO_BEHAVIOR_PATTERN_MESSAGE]

        summary = _behavior_summary(prepared)
        avg_time = summary["avg_time"]
        overall_pace = (
            "very compressed"
            if summary["compressed_attempt"]
            else "steady"
        )
        behavior_text = ""
        for idx, question in enumerate(prepared, 1):
            behavior_text += (
                f"\n{idx}. Topic: {question['topic']}"
                f" | Difficulty: {question['difficulty']}"
                f" | Correct: {'yes' if question['is_correct'] else 'no'}"
                f" | Relative pace: "
                f"{'faster than usual' if question['time_taken_seconds'] and question['time_taken_seconds'] < avg_time * 0.5 else 'slower than usual' if question['time_taken_seconds'] > avg_time * 1.6 else 'near usual pace'}"
                f" | Answer changed: {'yes' if question['answer_changed'] else 'no'}"
            )

        prompt = f"""You are analyzing student test-taking behavior in an academic testing platform.

Your job is to detect only repeated behavior patterns.

Important rules:
- Only report a pattern if it repeats across multiple questions.
- Do not mention exact numbers, timings, marks, or statistics.
- Do not give generic advice.
- Use simple student-friendly language.
- If the whole attempt looks very compressed for the number of questions and many answers are wrong, treat that as a meaningful rushing pattern even when the student is internally consistent.
- If there is not enough evidence for a strong repeated pattern, return exactly:
{NO_BEHAVIOR_PATTERN_MESSAGE}

Overall attempt pacing: {overall_pace}

Behavior data:
{behavior_text}

Return format:
- One bullet per repeated pattern
- Maximum 4 bullets
- Every line must start with "-"
- Each bullet must describe a repeated behavior pattern clearly
"""

        response = model.generate_content(prompt)
        result = getattr(response, "text", "").strip()
        parsed = _extract_bullets_only(result, 4, NO_BEHAVIOR_PATTERN_MESSAGE)
        return parsed if parsed != [NO_BEHAVIOR_PATTERN_MESSAGE] else _heuristic_test_behavior(prepared)
    except Exception:
        return _heuristic_test_behavior(prepared)


def build_ai_tutor_context(student_name, subjects_with_data):
    return (
        f"You are a personalized AI Tutor helping {student_name} improve academically.\n\n"
        "You must behave like a pattern detection engine.\n"
        "Only describe repeated and meaningful patterns.\n"
        "Never invent insights when evidence is weak.\n"
        "Do not mention marks, percentages, or raw statistics inside insight bullets.\n"
        "Keep the language clear, supportive, and student-friendly."
    )


def build_student_context(name, class_name, structured_marks, gender=None, parent_number=None, subject_focus=None, subject_insights=None):
    """
    Build focused tutoring context while still allowing concept explanations and follow-up questions.
    """
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
    subject_insights_text = f"\n\nCurrent Subject Insights:\n{subject_insights}" if subject_insights else ""

    return (
        f"You are a student-facing Academic Tutor AI helping {name} improve based on real test evidence.\n\n"
        "Student profile:\n"
        f"Name: {name}\n"
        f"Class: {class_name}\n"
        f"Gender: {gender or 'N/A'}\n"
        f"Parent Contact: {parent_number or 'N/A'}{subject_focus_text}\n\n"
        "Exam history:\n"
        f"{marks_table}{subject_insights_text}\n\n"
        "Your responsibilities:\n"
        "- Explain weak concepts related to the student's actual mistakes\n"
        "- Answer follow-up questions about the student's weak topics and learning patterns\n"
        "- Use the student's real academic data\n"
        "- Stay specific, simple, and useful\n\n"
        "Allowed:\n"
        "- Explaining concepts related to mistakes\n"
        "- Clarifying weak topics\n"
        "- Suggesting focused next steps based on actual patterns\n"
        "- Answering follow-up academic questions related to the same subjects\n\n"
        "Not allowed:\n"
        "- Unrelated chat, jokes, games, or off-topic discussion\n"
        "- Made-up insights without evidence\n"
        "- Generic filler advice\n\n"
        "If the user asks something completely unrelated, respond with:\n"
        "\"I'm here to help with your studies, mistakes, and weak topics. Ask me about a subject or pattern from your test data.\""
    )


def chat_with_student_context(student_context, conversation_history):
    """
    Run tutoring chat with student context injected into history.
    """
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
                    "Understood. I will help with this student's real mistakes, weak topics, and follow-up academic questions only."
                ],
            },
        ]

        for turn in conversation_history[:-1]:
            context_history.append({
                "role": turn["role"],
                "parts": [turn["parts"][0]],
            })

        chat = model.start_chat(history=context_history)
        latest_message = conversation_history[-1]["parts"][0]
        response = chat.send_message(latest_message)
        return response.text
    except Exception as exc:
        return f"Error: {str(exc)}"


def fallback_student_chat_response(student_name, subject_name, subject_insights, latest_message):
    subject = _normalize_text(subject_name) or "this subject"
    insights = subject_insights if isinstance(subject_insights, dict) else {}
    latest = _normalize_text(latest_message).lower()

    avg_score = insights.get("avg_score")
    recent_score = insights.get("recent_score")
    conceptual = _clean_pattern_list(insights.get("conceptual_mistakes") or [], NO_CONCEPT_PATTERN_MESSAGE)
    behavior = _clean_pattern_list(insights.get("behavior_patterns") or [], NO_BEHAVIOR_PATTERN_MESSAGE)

    valid_concept = conceptual[0] if conceptual != [NO_CONCEPT_PATTERN_MESSAGE] else ""
    valid_behavior = behavior[0] if behavior != [NO_BEHAVIOR_PATTERN_MESSAGE] else ""

    lines = [f"Here is a focused review for {subject}, {student_name}:"]

    if avg_score is not None and recent_score is not None:
        lines.append(f"- Your recent performance in {subject} is still close to your overall pattern in that subject.")
    elif recent_score is not None:
        lines.append(f"- Your latest result in {subject} is the best guide for what to revise next.")

    if any(word in latest for word in ["mistake", "wrong", "error", "concept", "why"]):
        if valid_concept:
            lines.append(valid_concept)
        else:
            lines.append(f"- There is not enough repeated mistake evidence yet in {subject} to identify a strong concept pattern.")
    elif any(word in latest for word in ["time", "behavior", "quick", "slow", "rush", "pace"]):
        if valid_behavior:
            lines.append(valid_behavior)
        else:
            lines.append(f"- There is not enough repeated behavior evidence yet in {subject} to identify a strong pacing pattern.")
    else:
        if valid_concept:
            lines.append(valid_concept)
        if valid_behavior:
            lines.append(valid_behavior)
        if not valid_concept and not valid_behavior:
            lines.append(f"- There is not enough repeated evidence yet in {subject} to identify a strong pattern.")

    lines.append(f"- Ask about a specific weak topic in {subject} if you want a more targeted explanation.")
    return "\n".join(lines)
