from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view
from django.shortcuts import get_object_or_404
from django.contrib.auth.hashers import check_password, make_password

from .models import Student, TestMark, Attendance, Subject, UpcomingTest, Notification
from .serializers import StudentSerializer, TestMarkSerializer, AttendanceSerializer, SubjectSerializer, UpcomingTestSerializer, NotificationSerializer
from tracker.ai_core.logic import build_student_context, chat_with_student_context


def _normalize_subject(subject):
    value = str(subject or '').strip().lower()
    return 'general' if not value else ' '.join(value.split())


def _subject_label(subject):
    value = str(subject or '').strip()
    if not value:
        return 'General'
    return ' '.join(word.capitalize() for word in value.split())


def _pct(mark):
    if not mark.total_marks:
        return 0
    return round((mark.marks_obtained / mark.total_marks) * 100)


def _create_notification(student, recipient_role, notification_type, subject, message, event_key, details=None):
    if Notification.objects.filter(event_key=event_key).exists():
        return None

    return Notification.objects.create(
        student=student,
        recipient_role=recipient_role,
        type=notification_type,
        subject=subject,
        message=message,
        event_key=event_key,
        details=details or {},
    )


def _run_performance_analysis(new_mark):
    student = new_mark.student
    subject_key = _normalize_subject(new_mark.subject)
    subject_label = _subject_label(new_mark.subject)

    all_marks = list(TestMark.objects.filter(student=student).order_by('date_taken', 'id'))
    subject_marks = [m for m in all_marks if _normalize_subject(m.subject) == subject_key]

    if not subject_marks:
        return

    subject_scores = [_pct(m) for m in subject_marks]
    latest_score = subject_scores[-1]
    previous_score = subject_scores[-2] if len(subject_scores) >= 2 else None
    overall_scores = [_pct(m) for m in all_marks]

    sudden_drop = previous_score is not None and (previous_score - latest_score) >= 15
    gradual_drop = len(subject_scores) >= 3 and subject_scores[-3] > subject_scores[-2] > subject_scores[-1]

    overall_avg = round(sum(overall_scores) / len(overall_scores)) if overall_scores else 0
    subject_avg = round(sum(subject_scores) / len(subject_scores)) if subject_scores else 0
    weakness = (
        len(subject_scores) >= 2
        and overall_avg >= 70
        and subject_avg <= max(50, overall_avg - 20)
    )

    warning_message = None
    warning_pattern = None

    if sudden_drop:
        warning_pattern = 'sudden_drop'
        warning_message = (
            f'Your {subject_label} score dropped significantly in the latest test '
            f'({previous_score}% -> {latest_score}%). Please review your preparation.'
        )
    elif gradual_drop:
        warning_pattern = 'gradual_decline'
        warning_message = (
            f'Your performance in {subject_label} has been gradually decreasing across the last '
            f'three tests ({subject_scores[-3]}% -> {subject_scores[-2]}% -> {subject_scores[-1]}%). '
            f'Please revise this subject.'
        )
    elif weakness:
        warning_pattern = 'subject_weakness'
        warning_message = (
            f'You are performing well overall ({overall_avg}%), but {subject_label} is weaker '
            f'({subject_avg}%). Focus more on this subject.'
        )

    prior_warning_exists = Notification.objects.filter(
        student=student,
        recipient_role='student',
        type='ai_warning',
        subject=subject_label,
    ).exists()

    # Teacher escalation: warning existed earlier and still no improvement in next subject test.
    if prior_warning_exists and previous_score is not None and latest_score <= previous_score:
        _create_notification(
            student=student,
            recipient_role='teacher',
            notification_type='teacher_alert',
            subject=subject_label,
            message=(
                f'Student {student.name} has not improved in {subject_label} despite previous '
                f'performance warning ({previous_score}% -> {latest_score}%).'
            ),
            event_key=f'teacher_alert:mark:{new_mark.id}:student:{student.id}:subject:{subject_key}',
            details={
                'pattern': 'no_improvement_after_warning',
                'latest_score': latest_score,
                'previous_score': previous_score,
            },
        )

    if warning_message:
        _create_notification(
            student=student,
            recipient_role='student',
            notification_type='ai_warning',
            subject=subject_label,
            message=warning_message,
            event_key=f'ai_warning:mark:{new_mark.id}:student:{student.id}:subject:{subject_key}:pattern:{warning_pattern}',
            details={
                'pattern': warning_pattern,
                'latest_score': latest_score,
                'previous_score': previous_score,
                'overall_avg': overall_avg,
                'subject_avg': subject_avg,
            },
        )




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

    def perform_create(self, serializer):
        mark = serializer.save()
        _run_performance_analysis(mark)


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


class UpcomingTestListCreateView(generics.ListCreateAPIView):
    queryset = UpcomingTest.objects.all()
    serializer_class = UpcomingTestSerializer

    def get_queryset(self):
        queryset = UpcomingTest.objects.all()
        class_name = self.request.query_params.get('class_name')
        student_id = self.request.query_params.get('student_id')

        if student_id:
            try:
                student = Student.objects.get(pk=student_id)
                queryset = queryset.filter(class_name=student.class_name)
            except Student.DoesNotExist:
                return UpcomingTest.objects.none()
        elif class_name:
            queryset = queryset.filter(class_name=class_name)

        return queryset

    def perform_create(self, serializer):
        test = serializer.save()
        subject_label = _subject_label(test.subject or test.topic)

        students = Student.objects.filter(class_name=test.class_name)
        for student in students:
            _create_notification(
                student=student,
                recipient_role='student',
                notification_type='test',
                subject=subject_label,
                message=(
                    f'New test scheduled: {subject_label} - {test.test_name} on {test.test_date}. '
                    f'Start preparing early.'
                ),
                event_key=f'test_schedule:test:{test.id}:student:{student.id}',
                details={
                    'test_name': test.test_name,
                    'subject': subject_label,
                    'date': str(test.test_date),
                    'total_marks': test.total_marks,
                },
            )


class UpcomingTestRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = UpcomingTest.objects.all()
    serializer_class = UpcomingTestSerializer


class NotificationListView(generics.ListAPIView):
    queryset = Notification.objects.select_related('student').all()
    serializer_class = NotificationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        student_id = self.request.query_params.get('student_id')
        recipient = self.request.query_params.get('recipient')
        notification_type = self.request.query_params.get('type')
        subject = self.request.query_params.get('subject')
        unread_only = self.request.query_params.get('unread')

        if student_id:
            queryset = queryset.filter(student_id=student_id)
        if recipient in {'student', 'teacher'}:
            queryset = queryset.filter(recipient_role=recipient)
        if notification_type:
            queryset = queryset.filter(type=notification_type)
        if subject:
            queryset = queryset.filter(subject__iexact=subject.strip())
        if unread_only in {'1', 'true', 'yes'}:
            queryset = queryset.filter(read_status=False)

        return queryset


class NotificationMarkReadView(APIView):
    def patch(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk)
        notification.read_status = True
        notification.save(update_fields=['read_status'])
        return Response({'message': 'Notification marked as read.'})


class NotificationMarkAllReadView(APIView):
    def post(self, request):
        student_id = request.data.get('student_id')
        recipient = request.data.get('recipient', 'student')

        queryset = Notification.objects.all()
        if student_id:
            queryset = queryset.filter(student_id=student_id)
        if recipient in {'student', 'teacher'}:
            queryset = queryset.filter(recipient_role=recipient)

        updated = queryset.filter(read_status=False).update(read_status=True)
        return Response({'updated': updated})

#-----------------------------------Login Authentication for Students----------------------------------

@api_view(['POST'])
def StudentLoginView(request):
    email = request.data.get('email', '').strip().lower()
    password = request.data.get('password', '').strip()

    if not email or not password:
        return Response({'error': 'Email and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        student = Student.objects.get(student_email__iexact=email)

        # Legacy support: allows existing plain-text default values until changed.
        is_valid = check_password(password, student.student_password) or password == student.student_password
        if not is_valid:
            return Response({'error': 'Invalid email or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        return Response({
            'id': student.id,
            'name': student.name,
            'class_name': student.class_name,
            'roll_number': student.roll_number,
            'student_number': student.student_number,
            'student_email': student.student_email,
        })
    except Student.DoesNotExist:
        return Response({'error': 'Invalid email or password.'}, status=status.HTTP_401_UNAUTHORIZED)


@api_view(['POST'])
def StudentChangePasswordView(request):
    student_id = request.data.get('student_id')
    current_password = request.data.get('current_password', '').strip()
    new_password = request.data.get('new_password', '').strip()

    if not student_id or not current_password or not new_password:
        return Response({'error': 'student_id, current_password and new_password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if len(new_password) < 6:
        return Response({'error': 'New password must be at least 6 characters.'}, status=status.HTTP_400_BAD_REQUEST)

    student = get_object_or_404(Student, pk=student_id)
    current_ok = check_password(current_password, student.student_password) or current_password == student.student_password

    if not current_ok:
        return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_401_UNAUTHORIZED)

    student.student_password = make_password(new_password)
    student.save(update_fields=['student_password'])

    return Response({'message': 'Password updated successfully.'}, status=status.HTTP_200_OK)