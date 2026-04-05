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


def _looks_generic_concept_response(bullets):
    generic_phrases = [
        "narrow down the options",
        "switch away from the correct line of thinking",
        "familiar-looking options",
        "some of the misses are on short, direct questions",
        "underlying concept",
        "what the question is actually asking",
        "conceptual understanding",
        "uncertainty in the underlying concept",
        "same wrong idea",
    ]
    text = " ".join(str(item or "").lower() for item in bullets or [])
    return any(phrase in text for phrase in generic_phrases)


def _option_label(option_key, options):
    if not isinstance(options, dict):
        return str(option_key or "").strip()
    label = str(options.get(str(option_key), "") or "").strip()
    if label:
        return f"{str(option_key).strip()} - {label}"
    return str(option_key or "").strip()


def _mcq_concept_frame(question):
    options = question.get("options") or {}
    selected = _normalize_text(question.get("selected_answer"))
    correct = _normalize_text(question.get("correct_answer"))
    question_text = _normalize_text(question.get("question_text"))

    selected_text = _normalize_text(options.get(selected, "")) if isinstance(options, dict) else ""
    correct_text = _normalize_text(options.get(correct, "")) if isinstance(options, dict) else ""

    if selected and correct and (selected_text or correct_text):
        return (
            f"you chose option {selected}{f' ({selected_text})' if selected_text else ''}, "
            f"but the correct choice is option {correct}{f' ({correct_text})' if correct_text else ''}"
        )
    if selected and correct:
        return f"you chose option {selected}, but the correct choice is option {correct}"
    if selected_text and correct_text:
        return f"you chose '{selected_text}', but the correct choice is '{correct_text}'"
    if question_text:
        return f"this question asks: {question_text[:100]}"
    return "the selected option does not match the concept tested by the question"


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
    topic_buckets = {}
    same_answer_groups = {}
    mcq_rows = []
    for index, question in enumerate(incorrect, start=1):
        selected = _normalize_text(question.get("selected_answer")).lower()
        correct = _normalize_text(question.get("correct_answer")).lower()
        topic = _normalize_topic(question.get("topic"))
        key = (selected, correct)
        same_answer_groups.setdefault(key, []).append(topic)
        topic_buckets.setdefault(topic, []).append((index, question))
        if str(question.get("question_type", "")).strip().upper() == "MCQ" or question.get("options"):
            mcq_rows.append((index, question))

    def _question_snippet(question):
        text = _normalize_text(question.get("question_text"))
        if not text:
            return "this question"
        return text if len(text) <= 90 else f"{text[:87]}..."

    repeated_choice_confusion = [
        (key, topics)
        for key, topics in same_answer_groups.items()
        if key[0] and key[1] and len(topics) >= 2
    ]
    if repeated_choice_confusion:
        (selected, correct), topics = repeated_choice_confusion[0]
        topic_label = _normalize_text(topics[0]) if topics else "these topics"
        bullets.append(
            f"- In multiple {topic_label} questions, you are choosing '{selected}' when the correct answer is '{correct}'. That points to one repeated misconception rather than random guessing."
        )

    if mcq_rows:
        option_insights = []
        for index, question in mcq_rows[:4]:
            frame = _mcq_concept_frame(question)
            topic = _normalize_topic(question.get("topic"))
            question_text = _normalize_text(question.get("question_text"))
            prefix = f"Q{index} ({topic})"
            if question_text:
                prefix += f" - '{question_text[:70] + ('...' if len(question_text) > 70 else '')}'"
            option_insights.append(f"{prefix}: {frame}")

        if option_insights:
            bullets.append(
                "- Decision-making evidence from your MCQ choices: "
                + "; ".join(option_insights[:2])
                + "."
            )
            selected_examples = []
            for index, question in mcq_rows[:3]:
                selected = _normalize_text(question.get("selected_answer"))
                correct = _normalize_text(question.get("correct_answer"))
                options = question.get("options") or {}
                selected_text = _normalize_text(options.get(selected, "")) if isinstance(options, dict) else ""
                correct_text = _normalize_text(options.get(correct, "")) if isinstance(options, dict) else ""
                if selected_text and correct_text:
                    selected_examples.append(
                        f"Q{index}: option {selected} ({selected_text}) vs option {correct} ({correct_text})"
                    )
                elif selected and correct:
                    selected_examples.append(f"Q{index}: option {selected} vs option {correct}")

            if selected_examples:
                bullets.append(
                    "- MCQ choice logic is the main issue here: "
                    + "; ".join(selected_examples[:2])
                    + ". That means the problem is not writing an answer, but deciding which option actually matches the concept."
                )
            first_index, first_question = mcq_rows[0]
            first_topic = _normalize_topic(first_question.get("topic"))
            first_question_text = _normalize_text(first_question.get("question_text"))
            first_selected = _normalize_text(first_question.get("selected_answer"))
            first_correct = _normalize_text(first_question.get("correct_answer"))
            first_options = first_question.get("options") or {}
            selected_text = _normalize_text(first_options.get(first_selected, "")) if isinstance(first_options, dict) else ""
            correct_text = _normalize_text(first_options.get(first_correct, "")) if isinstance(first_options, dict) else ""
            bullets.append(
                f"- In Q{first_index} ({first_topic}), the question tests a specific condition, but your selected option {first_selected or '[blank]'}"
                + (f" ({selected_text})" if selected_text else "")
                + " aligns with a related idea, not the exact requirement. "
                + f"The correct option {first_correct or '[blank]'}"
                + (f" ({correct_text})" if correct_text else "")
                + " is right because it directly satisfies that condition"
                + (f" in '{first_question_text[:80] + ('...' if len(first_question_text) > 80 else '')}'" if first_question_text else "")
                + "."
            )

    changed_wrong = sum(1 for question in incorrect if question.get("answer_changed"))
    if changed_wrong >= 2:
        changed_examples = [
            f"Q{index} ({_normalize_topic(question.get('topic'))}) - '{_question_snippet(question)}'"
            for index, question in enumerate(incorrect, start=1)
            if question.get("answer_changed")
        ][:2]
        example_text = f" Examples: {'; '.join(changed_examples)}." if changed_examples else ""
        bullets.append(
            f"- You are changing answers after narrowing down choices, which shows hesitation rather than settled concept knowledge.{example_text}"
        )

    difficulty_wrong = {"easy": 0, "medium": 0, "hard": 0}
    for question in incorrect:
        difficulty_wrong[_normalize_difficulty(question.get("difficulty"))] += 1

    if difficulty_wrong["easy"] >= 2:
        easy_examples = [
            f"Q{index} ({_normalize_topic(question.get('topic'))}) - '{_question_snippet(question)}'"
            for index, question in enumerate(incorrect, start=1)
            if _normalize_difficulty(question.get("difficulty")) == "easy"
        ][:2]
        example_text = f" Examples: {'; '.join(easy_examples)}." if easy_examples else ""
        bullets.append(
            f"- Some mistakes are happening on simpler questions too, so the issue looks conceptual and foundational rather than just exam pressure.{example_text}"
        )

    if mcq_rows:
        concept_pairs = []
        for _, question in mcq_rows:
            selected = _normalize_text(question.get("selected_answer"))
            correct = _normalize_text(question.get("correct_answer"))
            options = question.get("options") or {}
            selected_text = _normalize_text(options.get(selected, "")) if isinstance(options, dict) else ""
            correct_text = _normalize_text(options.get(correct, "")) if isinstance(options, dict) else ""
            if selected and correct and selected != correct:
                concept_pairs.append((selected_text or selected, correct_text or correct))

        if concept_pairs:
            sample_wrong, sample_right = concept_pairs[0]
            bullets.append(
                f"- Your MCQ pattern suggests you are mixing up {sample_wrong} with {sample_right}. The wrong option looks familiar, but the stem is testing the finer distinction between those two ideas."
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
INPUT:
You will receive incorrect responses with:
- question_text
- selected_answer
- correct_answer
- topic
- question_type
- options when available
3. Identify the thinking error behind the mistake.

- Only report a pattern if there is enough evidence across multiple incorrect answers.
- Every bullet must reference the student's actual answer sheet evidence, such as a question theme, the selected answer, the correct answer, or a clear answer-change pattern.
- For MCQs, analyze decision-making: explain why the selected option looked attractive, why the correct option is better, and what concept distinction was missed.
- Do not write generic 'revise the topic' language for MCQs.
- If options are provided, use them to explain the trap or misconception behind the wrong choice.

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
        if parsed != [NO_CONCEPT_PATTERN_MESSAGE] and not _looks_generic_concept_response(parsed):
            return parsed
        return _heuristic_conceptual_mistakes(prepared)
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


def comprehensive_answer_analysis(questions_data):
    """Deep conceptual analysis for every test attempt using question-level evidence."""
    if not questions_data or len(questions_data) < 3:
        return "No deep conceptual pattern detected from current answer script."

    prepared = []
    for idx, question in enumerate(questions_data, start=1):
        prepared.append({
            "index": idx,
            "topic": _normalize_topic(question.get("topic")),
            "difficulty": _normalize_difficulty(question.get("difficulty")),
            "question_text": _normalize_text(question.get("question_text"))[:220],
            "selected_answer": _normalize_text(question.get("selected_answer")),
            "correct_answer": _normalize_text(question.get("correct_answer")),
            "is_correct": bool(question.get("is_correct", False)),
            "answer_changed": bool(question.get("answer_changed", False)),
        })

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _fallback_comprehensive_analysis(prepared)

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.3},
        )

        rows = []
        for row in prepared:
            rows.append(
                f"Q{row['index']} | {row['topic']} | {row['difficulty']} | "
                f"{'correct' if row['is_correct'] else 'incorrect'} | "
                f"Selected: {row['selected_answer'] or '[blank]'} | "
                f"Correct: {row['correct_answer'] or '[n/a]'} | "
                f"Changed: {'yes' if row['answer_changed'] else 'no'} | "
                f"Question: {row['question_text'] or '[n/a]'}"
            )

        prompt = f"""You are an advanced Academic Evaluation AI.

Your task is NOT to summarize or give generic feedback.
Your task is to deeply ANALYZE a student's actual answer script and identify their REAL conceptual understanding.

OBJECTIVE:
- Understand what the student actually wrote
- Compare it with expected answers
- Identify missing concepts
- Detect misunderstood topics
- Detect skipped or weak sections

STRICT RULES:
- DO NOT give generic advice
- DO NOT mention marks or grades
- Every conclusion must be evidence-based and reference specific questions
- If evidence is insufficient, return exactly: No deep conceptual pattern detected from current answer script.

OUTPUT FORMAT:
## Deep Conceptual Analysis
- [Topic Weakness] ...with evidence
- [Missing Concepts] ...specific concept gaps
- [Answer Quality Issue] ...vague/incomplete reasoning

## Knowledge Coverage Summary
- Strong Areas:
- Weak Areas:
- Skipped/Partially Answered Areas:

## Thinking Pattern Observed
- Diagnose if memorizing / partial understanding / core misunderstanding

Student question-level evidence:
{chr(10).join(rows)}
"""

        response = model.generate_content(prompt)
        result = _normalize_text(getattr(response, "text", ""))
        if not result:
            return _fallback_comprehensive_analysis(prepared)
        return result.splitlines()
    except Exception:
        return _fallback_comprehensive_analysis(prepared)


def _fallback_comprehensive_analysis(prepared_rows):
    if not prepared_rows:
        return ["No deep conceptual pattern detected from current answer script."]

    by_topic = {}
    skipped = []
    for row in prepared_rows:
        topic = row["topic"]
        by_topic.setdefault(topic, {"total": 0, "correct": 0, "wrong": []})
        by_topic[topic]["total"] += 1
        if row["is_correct"]:
            by_topic[topic]["correct"] += 1
        else:
            by_topic[topic]["wrong"].append(row)
        if not row["selected_answer"]:
            skipped.append(f"Q{row['index']} ({topic})")

    strong_areas = []
    weak_areas = []
    deep_lines = ["## Deep Conceptual Analysis"]

    for topic, stats in by_topic.items():
        total = stats["total"]
        correct = stats["correct"]
        wrong = stats["wrong"]
        if total >= 2 and correct == total:
            strong_areas.append(topic)
        if wrong:
            weak_areas.append(topic)
            evidence = []
            for row in wrong[:2]:
                evidence.append(
                    f"Q{row['index']} selected '{row['selected_answer'] or '[blank]'}' instead of '{row['correct_answer'] or '[n/a]'}'"
                )
            deep_lines.append(
                f"- [Topic Weakness] {topic}: repeated confusion seen in {', '.join(evidence)}."
            )

    if not weak_areas and not skipped:
        return ["No deep conceptual pattern detected from current answer script."]

    deep_lines.append("- [Missing Concepts] Incorrect responses indicate concept-level gaps in the weak areas listed below.")
    if skipped:
        deep_lines.append(f"- [Answer Quality Issue] Some responses were skipped or blank: {', '.join(skipped[:5])}.")

    summary_lines = [
        "## Knowledge Coverage Summary",
        f"- Strong Areas: {', '.join(strong_areas) if strong_areas else 'No clearly strong area from current script.'}",
        f"- Weak Areas: {', '.join(weak_areas) if weak_areas else 'No repeated weak area detected.'}",
        f"- Skipped/Partially Answered Areas: {', '.join(skipped[:5]) if skipped else 'No fully skipped answer detected.'}",
    ]

    if weak_areas and strong_areas:
        thinking = "- The student shows partial understanding: stable in some topics but repeats concept-level errors in others."
    elif weak_areas:
        thinking = "- The student appears to rely on partial recall and misses core conceptual distinctions across multiple questions."
    else:
        thinking = "- No deep conceptual pattern detected from current answer script."

    thinking_lines = [
        "## Thinking Pattern Observed",
        thinking,
    ]

    return deep_lines + [""] + summary_lines + [""] + thinking_lines


def _concept_match(expected_concept, answer_text):
    concept = _normalize_text(expected_concept).lower()
    answer = _normalize_text(answer_text).lower()
    if not concept:
        return "missing"
    if concept in answer:
        return "covered"

    concept_tokens = [token for token in concept.replace("-", " ").split() if len(token) >= 4]
    if concept_tokens and any(token in answer for token in concept_tokens):
        return "partial"
    return "missing"


def _build_deep_analysis_fallback(questions, answer_map):
    topic_evidence = {}
    topic_missing = {}
    strong_topics = set()
    weak_topics = set()
    skipped = []
    quality_issues = []

    for idx, question in enumerate(questions, start=1):
        question_text = _normalize_text(question.get("question_text"))
        topic = _normalize_topic(question.get("topic"))
        expected = question.get("expected_concepts") or []
        if not isinstance(expected, list):
            expected = [str(expected)]

        answer = _normalize_text(
            answer_map.get(str(idx), "")
            or answer_map.get(question_text, "")
            or answer_map.get(str(question.get("question_id", "")), "")
            or question.get("student_answer", "")
        )

        topic_evidence.setdefault(topic, [])
        topic_missing.setdefault(topic, {})

        if not answer:
            skipped.append(f"Q{idx} ({topic}): no answer was written.")
            topic_evidence[topic].append(f"Q{idx} was skipped.")
            weak_topics.add(topic)
            continue

        if len(answer.split()) < 8:
            quality_issues.append(
                f"Q{idx} ({topic}) is very short and vague, suggesting incomplete reasoning."
            )

        covered = 0
        partial = 0
        missing = 0
        missing_concepts = []

        for concept in expected:
            status = _concept_match(concept, answer)
            if status == "covered":
                covered += 1
            elif status == "partial":
                partial += 1
            else:
                missing += 1
                missing_concepts.append(str(concept))
                topic_missing[topic][str(concept)] = topic_missing[topic].get(str(concept), 0) + 1

        topic_evidence[topic].append(
            f"Q{idx}: covered={covered}, partial={partial}, missing={missing}."
        )

        if expected and covered >= max(1, int(len(expected) * 0.7)) and missing == 0:
            strong_topics.add(topic)
        if missing > 0 or partial > 0:
            weak_topics.add(topic)
            if missing_concepts:
                quality_issues.append(
                    f"Q{idx} ({topic}) misses concepts: {', '.join(missing_concepts[:4])}."
                )

    if not weak_topics and not quality_issues and not skipped:
        return "No deep conceptual pattern detected from current answer script."

    deep_lines = ["## Deep Conceptual Analysis"]
    for topic in sorted(weak_topics):
        repeated_missing = [
            concept for concept, count in topic_missing.get(topic, {}).items() if count >= 2
        ]
        repeated_text = (
            f"Repeated missing concepts: {', '.join(repeated_missing[:4])}."
            if repeated_missing
            else "Missing concepts are spread across multiple questions in this topic."
        )
        evidence_text = " ".join(topic_evidence.get(topic, [])[:3])
        deep_lines.append(
            f"- [{topic} Weakness]: {repeated_text} Evidence: {evidence_text}"
        )

    if quality_issues:
        for issue in quality_issues[:5]:
            deep_lines.append(f"- [Answer Quality Issue]: {issue}")

    coverage_lines = [
        "## Knowledge Coverage Summary",
        f"- Strong Areas: {', '.join(sorted(strong_topics)) if strong_topics else 'No clearly strong topic from current script.'}",
        f"- Weak Areas: {', '.join(sorted(weak_topics)) if weak_topics else 'No repeated weak topic detected.'}",
        f"- Skipped/Partially Answered Areas: {'; '.join(skipped[:5]) if skipped else 'No fully skipped answer detected.'}",
    ]

    thinking_lines = ["## Thinking Pattern Observed"]
    if skipped and weak_topics:
        thinking_lines.append(
            "- The script suggests partial understanding with avoidance of concept-heavy parts, not full conceptual command."
        )
    elif weak_topics and quality_issues:
        thinking_lines.append(
            "- The student appears to know fragments of topics but struggles to connect definitions, reasoning, and complete explanations."
        )
    elif weak_topics:
        thinking_lines.append(
            "- The student shows partial topic familiarity but recurring conceptual omissions across related questions."
        )
    else:
        thinking_lines.append(
            "- No deep conceptual pattern detected from current answer script."
        )

    return "\n".join(deep_lines + [""] + coverage_lines + [""] + thinking_lines)


def deep_answer_script_analysis(questions, student_answers):
    if not isinstance(questions, list) or not questions:
        return "No deep conceptual pattern detected from current answer script."

    answer_map = {}
    if isinstance(student_answers, dict):
        answer_map = {str(k): _normalize_text(v) for k, v in student_answers.items()}
    elif isinstance(student_answers, list):
        for idx, item in enumerate(student_answers, start=1):
            if isinstance(item, str):
                answer_map[str(idx)] = _normalize_text(item)
                continue
            if not isinstance(item, dict):
                continue
            answer_text = _normalize_text(item.get("answer_text") or item.get("answer") or item.get("content"))
            key_candidates = [
                str(item.get("question_id", "")),
                _normalize_text(item.get("question_text")),
                str(idx),
            ]
            for key in key_candidates:
                if key:
                    answer_map[key] = answer_text

    non_empty_answers = sum(1 for value in answer_map.values() if value)
    if non_empty_answers < 2:
        return "No deep conceptual pattern detected from current answer script."

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _build_deep_analysis_fallback(questions, answer_map)

        normalized_questions = []
        for idx, question in enumerate(questions, start=1):
            if not isinstance(question, dict):
                continue
            question_text = _normalize_text(question.get("question_text"))
            topic = _normalize_topic(question.get("topic"))
            expected = question.get("expected_concepts") or []
            if not isinstance(expected, list):
                expected = [str(expected)]
            answer = _normalize_text(
                question.get("student_answer")
                or answer_map.get(str(question.get("question_id", "")), "")
                or answer_map.get(question_text, "")
                or answer_map.get(str(idx), "")
            )
            normalized_questions.append({
                "index": idx,
                "question_text": question_text,
                "topic": topic,
                "expected_concepts": [str(x) for x in expected],
                "student_answer": answer,
            })

        if len(normalized_questions) < 2:
            return "No deep conceptual pattern detected from current answer script."

        payload = ""
        for row in normalized_questions:
            payload += (
                f"\nQ{row['index']}"
                f"\nQuestion: {row['question_text']}"
                f"\nTopic: {row['topic']}"
                f"\nExpected Concepts: {', '.join(row['expected_concepts']) if row['expected_concepts'] else 'None provided'}"
                f"\nStudent Answer: {row['student_answer'] or '[No answer]'}\n"
            )

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.35},
        )

        prompt = f"""You are an advanced Academic Evaluation AI.

Your task is NOT to summarize or give generic feedback.
Your task is to deeply ANALYZE a student's actual answer script and identify their REAL conceptual understanding.

OBJECTIVE:
- Understand what the student actually wrote
- Compare it with expected answers
- Identify missing concepts
- Detect misunderstood topics
- Detect skipped or weak sections

CORE RULES:
1) Perform concept matching for each question:
   - Correctly covered
   - Partially covered
   - Completely missing
2) Detect repeated conceptual gaps across all questions.
3) Detect irrelevant, vague, or avoidance-style answers.
4) Topic-level weakness mapping must include reason and evidence.
5) Every conclusion must cite evidence from specific question answers.

STRICT RULES:
- No generic advice
- No template feedback
- No marks or grades
- No scoring language

OUTPUT FORMAT (exact headings):
## Deep Conceptual Analysis
- [Topic Weakness]: explanation with evidence from multiple answers
- [Missing Concepts]: specific concepts not included
- [Answer Quality Issue]: vague/incomplete reasoning evidence

## Knowledge Coverage Summary
- Strong Areas:
- Weak Areas:
- Skipped/Partially Answered Areas:

## Thinking Pattern Observed
- Diagnose how the student thinks (memorization/partial understanding/misunderstanding) with evidence.

If evidence is insufficient, return exactly:
No deep conceptual pattern detected from current answer script.

Question paper and extracted student answers:
{payload}
"""

        response = model.generate_content(prompt)
        result = _normalize_text(getattr(response, "text", ""))
        if not result:
            return _build_deep_analysis_fallback(normalized_questions, answer_map)
        return result
    except Exception:
        return _build_deep_analysis_fallback(questions, answer_map)
