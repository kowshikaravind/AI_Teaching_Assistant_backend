from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from .models import Student, TestMark
from .serializers import StudentSerializer, TestMarkSerializer
from tracker.ai_core.logic import build_student_context, chat_with_student_context


class StudentListCreateView(generics.ListCreateAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer


class StudentRetriveDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer


class TestMarkListCreateView(generics.ListCreateAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer

    def get_queryset(self):
        student_id = self.request.query_params.get('student_id')
        if student_id:
            return TestMark.objects.filter(student_id=student_id)
        return TestMark.objects.all()


class TestMarkRetrieveDestroyView(generics.RetrieveDestroyAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer


class StudentChatView(APIView):
    """
    POST /api/students/<id>/chat/
    
    Body:
    {
        "history": [
            {"role": "user", "parts": ["How is this student doing in Math?"]},
            {"role": "model", "parts": ["Based on the data, the student scored..."]},
            {"role": "user", "parts": ["What should I do first?"]}   <-- latest message
        ]
    }
    
    Returns:
    {
        "reply": "AI response text..."
    }
    """
    def post(self, request, pk):
        # 1. Get student
        student = get_object_or_404(Student, pk=pk)

        # 2. Get conversation history from request body
        history = request.data.get("history", [])
        if not history:
            return Response({"error": "No conversation history provided."}, status=status.HTTP_400_BAD_REQUEST)

        # 3. Build structured marks with full context
        marks = TestMark.objects.filter(student=student).order_by('date_taken')
        structured_marks = []
        for mark in marks:
            percentage = round((mark.marks_obtained / mark.total_marks) * 100) if mark.total_marks > 0 else 0
            structured_marks.append({
                "subject": mark.subject,
                "test_name": mark.test_name,
                "date": str(mark.date_taken),
                "marks_obtained": mark.marks_obtained,
                "total_marks": mark.total_marks,
                "percentage": percentage
            })

        # 4. Build the system prompt with student data
        attendance = getattr(student, 'attendance', 85)
        system_prompt = build_student_context(
            name=student.name,
            class_name=student.class_name,
            attendance=attendance,
            structured_marks=structured_marks,
            gender=student.gender,
            parent_number=student.parent_number,
        )

        # 5. Send to Gemini with full history
        reply = chat_with_student_context(system_prompt, history)

        return Response({"reply": reply})