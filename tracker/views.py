from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view
from django.shortcuts import get_object_or_404

from .models import Student, TestMark, Attendance, Subject
from .serializers import StudentSerializer, TestMarkSerializer, AttendanceSerializer, SubjectSerializer
from tracker.ai_core.logic import build_student_context, chat_with_student_context




# ── STUDENT VIEWS ─────────────────────────────────────────────────────────────

class StudentListCreateView(generics.ListCreateAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer


class StudentRetriveDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer


# ── TEST MARK VIEWS ───────────────────────────────────────────────────────────

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


# ── ATTENDANCE VIEWS ──────────────────────────────────────────────────────────

class AttendanceBulkSaveView(APIView):
    """
    POST /api/attendance/save/

    Saves attendance for multiple students in one request.
    If a record already exists for that student + date, it updates it.
    If not, it creates a new one.

    Request body:
    {
        "date": "2026-03-09",
        "records": [
            {"student_id": 1, "status": "present"},
            {"student_id": 2, "status": "absent"},
            {"student_id": 3, "status": "not_marked"}
        ]
    }
    """
    def post(self, request):
        date = request.data.get('date')
        records = request.data.get('records', [])

        if not date:
            return Response({"error": "Date is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not records:
            return Response({"error": "No records provided."}, status=status.HTTP_400_BAD_REQUEST)

        saved = []
        errors = []

        for record in records:
            student_id = record.get('student_id')
            att_status = record.get('status', 'not_marked')

            try:
                student = Student.objects.get(pk=student_id)

                # update_or_create — safe to call multiple times for same date
                attendance, created = Attendance.objects.update_or_create(
                    student=student,
                    date=date,
                    defaults={'status': att_status}
                )
                saved.append({
                    "student_id": student_id,
                    "student_name": student.name,
                    "status": att_status,
                    "created": created
                })

            except Student.DoesNotExist:
                errors.append({"student_id": student_id, "error": "Student not found"})

        return Response({
            "date": date,
            "saved": len(saved),
            "errors": errors,
            "records": saved
        }, status=status.HTTP_200_OK)


class AttendanceSummaryView(APIView):
    """
    GET /api/students/<id>/attendance-summary/

    Returns attendance summary for a single student.
    Used by the AI chat to get real attendance data.

    Response:
    {
        "total_sessions": 20,
        "present": 16,
        "absent": 4,
        "percentage": 80,
        "recent_absences": ["2026-03-01", "2026-03-05"]
    }
    """
    def get(self, request, pk):
        student = get_object_or_404(Student, pk=pk)
        records = Attendance.objects.filter(student=student).order_by('date')

        total = records.count()
        present = records.filter(status='present').count()
        absent = records.filter(status='absent').count()
        percentage = round((present / total) * 100) if total > 0 else 0

        # Last 5 absent dates for the AI context
        recent_absences = list(
            records.filter(status='absent')
            .order_by('-date')
            .values_list('date', flat=True)[:5]
        )
        recent_absences = [str(d) for d in recent_absences]

        return Response({
            "total_sessions": total,
            "present": present,
            "absent": absent,
            "percentage": percentage,
            "recent_absences": recent_absences,
        })


class AttendanceByDateView(APIView):
    """
    GET /api/attendance/?date=2026-03-09&class_name=11-SectionA

    Returns all attendance records for a given date (and optionally class).
    Used to reload previously saved attendance when the teacher reopens a date.
    """
    def get(self, request):
        date = request.query_params.get('date')
        class_name = request.query_params.get('class_name')

        if not date:
            return Response({"error": "Date is required."}, status=status.HTTP_400_BAD_REQUEST)

        records = Attendance.objects.filter(date=date).select_related('student')

        if class_name:
            records = records.filter(student__class_name=class_name)

        result = [
            {
                "student_id": r.student.id,
                "student_name": r.student.name,
                "status": r.status
            }
            for r in records
        ]

        return Response({"date": date, "records": result})


# ── AI CHAT VIEW ──────────────────────────────────────────────────────────────

class StudentChatView(APIView):
    """
    POST /api/students/<id>/chat/

    Body:
    {
        "history": [
            {"role": "user", "parts": ["How is this student doing?"]},
            {"role": "model", "parts": ["Based on the data..."]},
            {"role": "user", "parts": ["What subject needs attention?"]}
        ]
    }
    """
    def post(self, request, pk):
        student = get_object_or_404(Student, pk=pk)
        history = request.data.get("history", [])

        if not history:
            return Response({"error": "No conversation history provided."}, status=status.HTTP_400_BAD_REQUEST)

        # Build structured marks
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

        # Get REAL attendance from database
        att_records = Attendance.objects.filter(student=student)
        total_sessions = att_records.count()
        present_count = att_records.filter(status='present').count()
        attendance_pct = round((present_count / total_sessions) * 100) if total_sessions > 0 else 0

        # Build AI context with real attendance
        system_prompt = build_student_context(
            name=student.name,
            class_name=student.class_name,
            attendance=attendance_pct,
            structured_marks=structured_marks,
            gender=student.gender,
            parent_number=student.parent_number,
        )

        reply = chat_with_student_context(system_prompt, history)
        return Response({"reply": reply})



class ClassChatView(APIView):
    """
    POST /api/class-chat/

    A class-wide AI chat. The teacher can ask anything about
    any student or the whole class. All student data is injected
    as context so Gemini always has the full picture.

    Body:
    {
        "message": "Who is struggling the most?",
        "history": [...],           # previous messages in Gemini format
        "student_ids": [1, 2, 3]
    }
    """
    def post(self, request):
        message = request.data.get("message", "").strip()
        history = request.data.get("history", [])
        student_ids = request.data.get("student_ids", [])

        if not message:
            return Response({"error": "No message provided."}, status=status.HTTP_400_BAD_REQUEST)

        # ── BUILD CLASS CONTEXT ───────────────────────────────────
        students = Student.objects.filter(id__in=student_ids)
        lines = ["CLASS PERFORMANCE DATA\n" + "=" * 40]

        for student in students:
            marks = TestMark.objects.filter(student=student).order_by('date_taken')
            att = Attendance.objects.filter(student=student)
            total_att = att.count()
            present_att = att.filter(status='present').count()
            att_pct = round((present_att / total_att) * 100) if total_att > 0 else "No data"

            lines.append(f"\nStudent: {student.name} | Class: {student.class_name} | Attendance: {att_pct}%")

            if marks.exists():
                by_subject = {}
                for m in marks:
                    pct = round((m.marks_obtained / m.total_marks) * 100)
                    if m.subject not in by_subject:
                        by_subject[m.subject] = []
                    by_subject[m.subject].append(f"{pct}% ({m.date_taken})")

                for subject, scores in by_subject.items():
                    lines.append(f"  {subject}: {' → '.join(scores)}")
            else:
                lines.append("  No marks recorded yet.")

        class_context = "\n".join(lines)

        system_context = f"""You are an AI assistant helping a teacher understand their class performance.
You have access to real data for every student including their test scores per subject over time and attendance.

{class_context}

Answer the teacher's questions using this data. Be specific — mention student names, exact scores, and trends.
If asked about a specific student, focus on their data. If asked about the class, summarise across all students.
Keep answers clear and concise. Do not make up data that is not shown above."""

        # ── CALL GEMINI ───────────────────────────────────────────
        import google.generativeai as genai
        import os
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-2.5-flash")

        # Inject context as first exchange so Gemini always sees it
        full_history = [
            {"role": "user", "parts": [system_context]},
            {"role": "model", "parts": ["Understood. I have the full class data loaded. What would you like to know?"]},
            *history,
        ]

        chat = model.start_chat(history=full_history)
        response = chat.send_message(message)

        return Response({"reply": response.text})

class SubjectListCreateView(generics.ListCreateAPIView):
    queryset = Subject.objects.all().order_by('name')
    serializer_class = SubjectSerializer

class SubjectDeleteView(generics.DestroyAPIView):
    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer

#-----------------------------------Login Authentication for Students----------------------------------

@api_view(['POST'])
def StudentLoginView(request):
    roll_number = request.data.get('roll_number', '').strip()
    dob = request.data.get('dob', '').strip()

    if not roll_number or not dob:
        return Response({'error': 'Roll number and date of birth are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        student = Student.objects.get(roll_number=roll_number, dob=dob)
        return Response({
            'id': student.id,
            'name': student.name,
            'class_name': student.class_name,
            'roll_number': student.roll_number,
        })
    except Student.DoesNotExist:
        return Response({'error': 'Invalid roll number or date of birth.'}, status=status.HTTP_401_UNAUTHORIZED)