# backend/tracker/ai_core/logic.py
import os
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field

# 1. Setup
# Ensure your .env is in the root backend folder
load_dotenv()

# 2. Define Output Schema
class StudentAnalysis(BaseModel):
    student_name: str = Field(description="Name of the student")
    trend_direction: str = Field(description="One of: 'Improving 📈', 'Stable ➡️', or 'Declining 📉'")
    predicted_next_grade: int = Field(description="Prediction for the NEXT exam based on the trend")
    risk_status: str = Field(description="Risk Level: 'Safe', 'At Risk', or 'Critical'")
    remedial_action: str = Field(description="A specific action plan (max 10 words)")

# 3. The Function (Callable by Django)
def analyze_student(name, marks_list, attendance, notes=""):
    """
    Analyzes a single student and returns a dictionary.
    """
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return {"error": "Google API Key not found"}

        llm = ChatGoogleGenerativeAI(model="models/gemini-2.5-flash", temperature=0, google_api_key=api_key)
        parser = PydanticOutputParser(pydantic_object=StudentAnalysis)

        prompt_template = ChatPromptTemplate.from_messages([
            ("system", "You are an Academic AI. Analyze the student's historical test scores.\n"
                    "LOGIC RULES:\n"
                    "1. If the list is going up (e.g., 60, 70), Trend is 'Improving'.\n"
                    "2. If the list is going down (e.g., 40, 30), Trend is 'Declining'.\n"
                    "3. If Trend is 'Improving', predict a HIGHER grade next.\n"
                    "4. If Trend is 'Declining' OR Attendance < 70%, Risk is 'Critical'.\n"
                    "\n{format_instructions}"),
            ("human", "Name: {name}\n"
                    "Attendance: {attendance}%\n"
                    "Test History: {marks}\n" 
                    "Notes: {notes}")
        ])

        prompt = prompt_template.partial(format_instructions=parser.get_format_instructions())
        chain = prompt | llm | parser

        # Run for single student
        result = chain.invoke({
            "name": name,
            "marks": marks_list,
            "attendance": attendance,
            "notes": notes
        })
        
        # Return as dictionary so Django can serialize it to JSON
        return result.model_dump()

    except Exception as e:
        return {"error": str(e)}

# ... (rest of your code above) ...

# --- TEST BLOCK (Runs only when you run this file directly) ---
if __name__ == "__main__":
    print("🔬 Running AI Test Mode...")
    
    # Test Data
    test_student = {
        "name": "Test Student",
        "marks": [50, 45, 40],  # Declining trend
        "attendance": 60,       # Critical attendance
        "notes": "Struggling with basic concepts."
    }

    # Call the function
    result = analyze_student(
        test_student["name"], 
        test_student["marks"], 
        test_student["attendance"], 
        test_student["notes"]
    )

    # Print Result
    import json
    print(json.dumps(result, indent=2))