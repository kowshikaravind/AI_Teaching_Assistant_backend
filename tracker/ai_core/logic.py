import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()


def build_student_context(name, class_name, attendance, structured_marks, gender=None, parent_number=None):
    """
    Builds a detailed student profile string to inject into chat history.
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

    context = f"""Here is the complete profile of the student you will be analyzing:

=== STUDENT PROFILE ===
Name: {name}
Class: {class_name}
Gender: {gender or 'N/A'}
Attendance: {attendance}%
Parent Contact: {parent_number or 'N/A'}

=== EXAM HISTORY (chronological order) ===
{marks_table}

You are an expert Academic Counselor AI helping a Teaching Assistant.
Always refer to the student by their name ({name}) in your responses.
Be specific — reference actual test names, dates, scores, and subjects from the data above.
Never say you don't have information — all the data you need is above.
Keep responses clear and concise."""

    return context


def chat_with_student_context(student_context, conversation_history):
    """
    Sends the conversation to Gemini.
    
    Student context is injected as the FIRST message pair in history
    so Gemini always has the student data — regardless of system_instruction support.

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
        # This guarantees Gemini always knows the student profile,
        # even if system_instruction is ignored.
        context_history = [
            {
                "role": "user",
                "parts": [student_context]
            },
            {
                "role": "model",
                "parts": ["Understood. I have fully reviewed this student's profile and exam history. I'm ready to answer any questions about their academic performance."]
            }
        ]

        # Append all previous conversation turns EXCEPT the last one
        # (the last one is the new message we're about to send)
        for turn in conversation_history[:-1]:
            context_history.append({
                "role": turn["role"],       # "user" or "model"
                "parts": [turn["parts"][0]]
            })

        # Start chat with the full context + history
        chat = model.start_chat(history=context_history)

        # Send the latest user message
        latest_message = conversation_history[-1]["parts"][0]
        response = chat.send_message(latest_message)

        return response.text

    except Exception as e:
        return f"Error: {str(e)}"