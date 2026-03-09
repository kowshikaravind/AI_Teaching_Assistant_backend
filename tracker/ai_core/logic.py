import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()


def build_student_context(name, class_name, attendance, structured_marks, gender=None, parent_number=None):
    """
    Builds a detailed student profile string to inject into chat history
    as the first message so Gemini always knows the student context.
    """
    if structured_marks:
        marks_table = "Subject          | Test Name             | Date       | Score      | %\n"
        marks_table += "-" * 70 + "\n"
        for m in structured_marks:
            marks_table += (
                f"{m['subject']:<17}| {m['test_name']:<22}| {m['date']} | "
                f"{m['marks_obtained']}/{m['total_marks']:<8} | {m['percentage']}%\n"
            )
    else:
        marks_table = "No exam records available yet."

    context = f"""You are an expert Academic Counselor AI assistant helping a Teaching Assistant analyze and support their student.

You have been given the complete, up-to-date profile for the following student. Use this data to answer every question.

=== STUDENT PROFILE ===
Name: {name}
Class: {class_name}
Gender: {gender or 'N/A'}
Attendance: {attendance}%
Parent Contact: {parent_number or 'N/A'}

=== EXAM HISTORY (chronological order) ===
{marks_table}

=== YOUR ROLE ===
- Always refer to the student by their name ({name}) in your responses
- Be specific — reference actual test names, dates, scores, and subjects from the data
- Detect trends, patterns, and risks from the exam history
- Be honest — if a student is struggling, say so clearly
- If asked to draft a parent message, write it professionally
- If asked about predictions, base them strictly on the actual data above
- Keep responses concise but insightful — no unnecessary filler

=== STRICT BOUNDARIES ===
You are ONLY allowed to discuss topics directly related to:
- {name}'s academic performance, marks, scores, and test history
- Subject-specific strengths and weaknesses
- Study strategies and intervention advice for the teacher
- Parent communication drafts related to {name}
- Attendance and its impact on {name}'s performance
- Predictions or forecasts based on {name}'s data
- Meeting recommendations or action plans for the teacher

If the user asks ANYTHING outside of these topics — including general knowledge,
coding help, creative writing, jokes, unrelated questions, or anything not about
this student — you must respond with exactly this:
"I'm focused on {name}'s academic analysis. I can only help with questions 
about their performance, subjects, attendance, or how to support them. 
What would you like to know about {name}?"

Do not answer off-topic questions under any circumstances, even if the user insists."""

    return context


def chat_with_student_context(student_context, conversation_history):
    """
    Sends the conversation to Gemini with student context injected
    as the first message pair in history.

    conversation_history: list of dicts [{"role": "user"|"model", "parts": ["text"]}]
    The last item is the new user message to respond to.
    """
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return "Error: Google API Key not found."

        genai.configure(api_key=api_key)

        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config={"temperature": 0.4}
        )

        # Inject student context as the very first exchange in history.
        # This guarantees Gemini always knows the student profile
        # and the topic boundaries regardless of the conversation length.
        context_history = [
            {
                "role": "user",
                "parts": [student_context]
            },
            {
                "role": "model",
                "parts": [
                    "Understood. I have fully reviewed this student's profile and exam history. "
                    "I will only discuss topics related to this student's academic performance, "
                    "subjects, attendance, and support strategies. I'm ready to help."
                ]
            }
        ]

        # Append all previous conversation turns except the last one
        # (the last one is the new message we're about to send)
        for turn in conversation_history[:-1]:
            context_history.append({
                "role": turn["role"],
                "parts": [turn["parts"][0]]
            })

        # Start chat with full context + conversation history
        chat = model.start_chat(history=context_history)

        # Send the latest user message
        latest_message = conversation_history[-1]["parts"][0]
        response = chat.send_message(latest_message)

        return response.text

    except Exception as e:
        return f"Error: {str(e)}"