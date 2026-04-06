from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache
from django.db import connection, close_old_connections
import logging
import json
import threading
import re
import csv
import io

from .models import Student, TestMark, TestQuestion, Subject, UpcomingTest, Notification, AdminCredential, TeacherCredential, StudentTestResponse, TestResult, TestAttempt
from .serializers import StudentSerializer, TestMarkSerializer, SubjectSerializer, UpcomingTestSerializer, NotificationSerializer, TestQuestionSerializer
from .ai_core.logic import analyze_test_submission, build_student_context, chat_with_student_context, fallback_student_chat_response, validate_analysis_report

logger = logging.getLogger(__name__)
AI_UNAVAILABLE_MESSAGE = "AI analysis is temporarily unavailable."


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


def _student_has_submitted_test(student, test):
    if _table_exists('tracker_testattempt') and TestAttempt.objects.filter(student=student, test=test).exists():
        return True

    if _table_exists('tracker_testresult') and TestResult.objects.filter(student=student, test=test).exclude(status='InProgress').exists():
        return True

    normalized_subject = _normalize_subject(test.subject or test.topic or 'General')
    test_name_key = ' '.join(str(test.test_name or '').strip().lower().split())
    marks = TestMark.objects.filter(
        student=student,
        test_name=test.test_name,
        date_taken=test.test_date,
    )

    matched = any(
        _normalize_subject(mark.subject) == normalized_subject
        and ' '.join(str(mark.test_name or '').strip().lower().split()) == test_name_key
        for mark in marks
    )

    logger.warning(
        'TEST_SUBMISSION_MATCH_DEBUG student_id=%s test_id=%s subject=%s name=%s date=%s candidates=%s matched=%s',
        student.id,
        test.id,
        normalized_subject,
        test_name_key,
        str(test.test_date),
        marks.count(),
        matched,
    )

    return matched


def _workflow_status(test):
    if test.status == 'finished':
        return 'Completed'
    if test.status == 'active':
        return 'Active'
    return 'Published' if bool(test.questions_generated) else 'Draft'


def _coerce_aware_datetime(value):
    if value in (None, ''):
        return None

    if isinstance(value, timezone.datetime):
        dt = value
    else:
        dt = timezone.datetime.fromisoformat(str(value).replace('Z', '+00:00'))

    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())

    return dt


def _can_student_attempt_test(test, student, allow_expired_submission=False):
    now = timezone.now()

    if _student_has_submitted_test(student, test):
        return False, 'Test already submitted.'

    if test.start_time and now < test.start_time:
        return False, 'Test has not started yet.'

    if test.end_time and now > test.end_time and not allow_expired_submission:
        return False, 'Test time has expired.'

    return True, ''




# ── STUDENT VIEWS ─────────────────────────────────────────────────────────────

class StudentListCreateView(generics.ListCreateAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

    def get_queryset(self):
        """
        Filter students by assigned_class if provided in query params.
        Teachers see only students from their assigned class.
        """
        queryset = Student.objects.all()
        assigned_class = self.request.query_params.get('assigned_class')
        
        if assigned_class and assigned_class != 'Class N/A':
            # Filter by class_name matching assigned_class
            queryset = queryset.filter(class_name=assigned_class)
        
        return queryset


class StudentRetriveDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer


# ── TEST MARK VIEWS ───────────────────────────────────────────────────────────

class TestMarkListCreateView(generics.ListCreateAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer

    def get_queryset(self):
        student_id = self.request.query_params.get('student_id')
        assigned_class = self.request.query_params.get('assigned_class')

        if student_id:
            return TestMark.objects.filter(student_id=student_id)

        if assigned_class and assigned_class != 'Class N/A':
            return TestMark.objects.filter(student__class_name=assigned_class)

        return TestMark.objects.all()

    def perform_create(self, serializer):
        mark = serializer.save()
        _run_performance_analysis(mark)


class TestMarkRetrieveDestroyView(generics.RetrieveDestroyAPIView):
    queryset = TestMark.objects.all()
    serializer_class = TestMarkSerializer




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
        subject_focus = str(request.data.get("subject_name", "")).strip()
        subject_insights = request.data.get("subject_insights", {}) or {}

        if not history:
            return Response({"error": "No conversation history provided."}, status=status.HTTP_400_BAD_REQUEST)

        marks = TestMark.objects.filter(student=student).order_by('date_taken', 'id')
        structured_marks = []
        for mark in marks:
            percentage = round((mark.marks_obtained / mark.total_marks) * 100) if mark.total_marks > 0 else 0
            structured_marks.append({
                "subject": mark.subject,
                "test_name": mark.test_name,
                "date": str(mark.date_taken),
                "marks_obtained": mark.marks_obtained,
                "total_marks": mark.total_marks,
                "percentage": percentage,
            })

        insights_lines = []
        if isinstance(subject_insights, dict):
            for label, key in [
                ('Average Score', 'avg_score'),
                ('Recent Score', 'recent_score'),
                ('Strengths', 'strengths'),
                ('Weak Topics', 'weak_topics'),
                ('Critical Weak Areas', 'critical_weak_areas'),
                ('Conceptual Mistakes', 'conceptual_mistakes'),
                ('Behavior Patterns', 'behavior_patterns'),
                ('Mastery Summary', 'mastery_summary'),
                ('Personalized Feedback', 'personalized_feedback'),
                ('Improvement Plan', 'improvement_plan'),
                ('Practice Questions', 'practice_questions'),
            ]:
                value = subject_insights.get(key)
                if isinstance(value, list) and value:
                    insights_lines.append(f"{label}: {', '.join(str(item) for item in value[:5])}")
                elif isinstance(value, dict) and value:
                    insights_lines.append(f"{label}: {value}")
                elif value not in (None, '', []):
                    if key in {'avg_score', 'recent_score'}:
                        insights_lines.append(f"{label}: {value}%")
                    else:
                        insights_lines.append(f"{label}: {value}")

        system_prompt = build_student_context(
            name=student.name,
            class_name=student.class_name,
            structured_marks=structured_marks,
            gender=student.gender,
            parent_number=student.parent_number,
            subject_focus=subject_focus or None,
            subject_insights='\n'.join(insights_lines) if insights_lines else None,
        )

        reply = chat_with_student_context(system_prompt, history)
        if isinstance(reply, str) and reply.startswith('Error:'):
            latest_message = history[-1].get('parts', ['']) if history else ['']
            reply = fallback_student_chat_response(
                student_name=student.name,
                subject_name=subject_focus,
                subject_insights=subject_insights,
                latest_message=latest_message[0] if latest_message else '',
            )
        return Response({"reply": reply})


class StudentAITutorView(APIView):
    """
    GET /api/students/<id>/ai-tutor/
    
    Returns subject-wise analysis data for the AI Tutor page.
    
    Response:
    {
        "subjects": [
            {
                "name": "Mathematics",
                "test_count": 3,
                "avg_score": 75,
                "recent_score": 80,
                "conceptual_mistakes": ["Pattern 1", "Pattern 2"],
                "behavior_patterns": ["Pattern 1", "Pattern 2"]
            },
            ...
        ]
    }
    """
    def get(self, request, pk):
        student = get_object_or_404(Student, pk=pk)
        
        # Check if TestAttempt table exists before querying
        try:
            if _table_exists('tracker_testattempt'):
                attempts = list(
                    TestAttempt.objects
                    .filter(student=student)
                    .select_related('test')
                    .order_by('test__subject', 'test__test_date', 'test__id')
                )
            else:
                attempts = []
        except Exception:
            attempts = []
        
        marks = list(TestMark.objects.filter(student=student).order_by('date_taken', 'id'))

        grouped = {}
        attempt_keys = set()

        for attempt in attempts:
            subject = _subject_label(attempt.test.subject or attempt.test.topic or 'General')
            key = (
                _normalize_subject(subject),
                ' '.join(str(attempt.test.test_name or '').strip().lower().split()),
                str(attempt.test.test_date),
            )
            attempt_keys.add(key)
            grouped.setdefault(subject, []).append({
                'attempt': attempt,
                'test': attempt.test,
                'mark': None,
            })

        for mark in marks:
            subject = _subject_label(mark.subject or 'General')
            key = (
                _normalize_subject(subject),
                ' '.join(str(mark.test_name or '').strip().lower().split()),
                str(mark.date_taken),
            )
            if key in attempt_keys:
                continue

            test = (
                UpcomingTest.objects
                .filter(test_name=mark.test_name, test_date=mark.date_taken)
                .filter(subject__iexact=subject)
                .order_by('id')
                .first()
            )
            if not test:
                test = (
                    UpcomingTest.objects
                    .filter(test_name=mark.test_name, test_date=mark.date_taken)
                    .order_by('id')
                    .first()
                )

            grouped.setdefault(subject, []).append({
                'attempt': None,
                'test': test,
                'mark': mark,
            })

        subjects_data = []
        for subject, subject_attempts in grouped.items():
            subject_attempts.sort(
                key=lambda item: (
                    str(item['test'].test_date if item['test'] else item['mark'].date_taken),
                    int(item['test'].id if item['test'] else item['mark'].id),
                )
            )
            test_entries = []
            scores = []

            for item in subject_attempts:
                attempt = item['attempt']
                test = item['test']
                mark = item['mark']

                score = float((attempt.score if attempt else (mark.marks_obtained if mark else 0)) or 0)
                total_marks = float((attempt.total_marks if attempt else (mark.total_marks if mark else 0)) or 0)

                if total_marks:
                    pct = round((score / total_marks) * 100)
                    scores.append(pct)
                else:
                    pct = 0

                question_rows = _question_level_rows(test, student) if test else []
                analysis = analyze_test_submission(
                    question_rows,
                    student_name=student.name,
                    test_name=test.test_name if test else mark.test_name,
                    subject_name=(test.subject or test.topic or 'General') if test else mark.subject,
                    use_llm=False,
                )
                result_row = TestResult.objects.filter(student=student, test=test).order_by('-completed_at').first() if test else None
                conceptual_patterns = attempt.conceptual_patterns if attempt and isinstance(attempt.conceptual_patterns, list) else analysis.get('conceptual_patterns', [])
                behavior_patterns = attempt.behavior_patterns if attempt and isinstance(attempt.behavior_patterns, list) else analysis.get('behavior_patterns', [])
                strengths = result_row.strengths if result_row and isinstance(result_row.strengths, list) else analysis.get('strengths', [])
                mastery_summary = analysis.get('mastery_summary', AI_UNAVAILABLE_MESSAGE)
                comprehensive_analysis = analysis.get('comprehensive_analysis', ["Use the chat button for AI insights."])

                test_entries.append({
                    'test_id': test.id if test else f"legacy-{mark.id}",
                    'test_name': test.test_name if test else mark.test_name,
                    'test_date': test.test_date if test else mark.date_taken,
                    'score': score,
                    'total_marks': total_marks,
                    'percentage': pct,
                    'conceptual_mistakes': _pattern_message("No strong patterns detected yet.", conceptual_patterns),
                    'behavior_patterns': _pattern_message("No clear behavior patterns detected.", behavior_patterns),
                    'strengths': _strength_message("No strong strengths detected yet.", strengths),
                    'mastery_summary': mastery_summary,
                    'comprehensive_analysis': comprehensive_analysis if isinstance(comprehensive_analysis, list) else [comprehensive_analysis],
                    'performance_summary': analysis.get('performance_summary', {}),
                    'topic_analysis': analysis.get('topic_analysis', {}),
                    'mistake_breakdown': analysis.get('mistake_breakdown', {}),
                    'detailed_mistake_analysis': analysis.get('detailed_mistake_analysis', []),
                    'repeated_weakness': analysis.get('repeated_weakness'),
                    'personalized_feedback': analysis.get('personalized_feedback', ''),
                    'improvement_plan': analysis.get('improvement_plan', []),
                    'practice_questions': analysis.get('practice_questions', []),
                    'predicted_performance': result_row.predicted_performance if result_row and isinstance(result_row.predicted_performance, dict) else analysis.get('predicted_performance', {}),
                })

            for index, entry in enumerate(test_entries):
                previous_entry = test_entries[index - 1] if index > 0 else None
                entry['comparison'] = _build_comparison(entry, previous_entry)

            avg_score = round(sum(scores) / len(scores)) if scores else 0
            recent_score = scores[-1] if scores else 0
            latest_test = test_entries[-1] if test_entries else None

            subjects_data.append({
                'name': subject,
                'test_count': len(test_entries),
                'avg_score': avg_score,
                'recent_score': recent_score,
                'conceptual_mistakes': latest_test['conceptual_mistakes'] if latest_test else ["No review available."],
                'behavior_patterns': latest_test['behavior_patterns'] if latest_test else ["No review available."],
                'strengths': latest_test['strengths'] if latest_test else ["No review available."],
                'mastery_summary': latest_test['mastery_summary'] if latest_test else "No review available.",
                'personalized_feedback': latest_test['personalized_feedback'] if latest_test else "No review available.",
                'improvement_plan': latest_test['improvement_plan'] if latest_test else [],
                'practice_questions': latest_test['practice_questions'] if latest_test else [],
                'tests': list(reversed(test_entries)),
            })

        subjects_data.sort(key=lambda item: item['test_count'], reverse=True)
        return Response({'subjects': subjects_data})


class DeepAnswerScriptAnalysisView(APIView):
    """
    POST /api/answer-script/deep-analysis/

    Body:
    {
        "questions": [
            {
                "question_id": 1,
                "question_text": "...",
                "topic": "...",
                "expected_concepts": ["...", "..."]
            }
        ],
        "student_answers": [
            {"question_id": 1, "answer_text": "..."}
        ]
    }
    """

    def post(self, request):
        questions = request.data.get('questions') or request.data.get('rows') or []
        student_answers = request.data.get('student_answers', [])

        if not isinstance(questions, list) or not questions:
            return Response(
                {'error': 'questions must be a non-empty list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        answer_lookup = {}
        if isinstance(student_answers, dict):
            answer_lookup = {str(key): value for key, value in student_answers.items()}
        elif isinstance(student_answers, list):
            for row in student_answers:
                if not isinstance(row, dict):
                    continue
                qid = row.get('question_id', row.get('id'))
                if qid is None:
                    continue
                answer_lookup[str(qid)] = row

        normalized_rows = []
        for index, row in enumerate(questions, start=1):
            if not isinstance(row, dict):
                continue
            qid = row.get('question_id', row.get('id', index))
            answer_row = answer_lookup.get(str(qid), {}) if isinstance(answer_lookup, dict) else {}
            normalized_rows.append({
                'question_id': qid,
                'question_text': row.get('question_text') or row.get('question') or '',
                'student_answer': row.get('student_answer') or row.get('selected_answer') or row.get('answer') or answer_row.get('student_answer') or answer_row.get('answer') or answer_row.get('answer_text') or '',
                'correct_answer': row.get('correct_answer') or row.get('expected_answer') or '',
                'topic': row.get('topic') or row.get('subject') or 'General',
                'subtopic': row.get('subtopic') or row.get('sub_topic') or row.get('topic_detail') or '',
                'time_taken_seconds': row.get('time_taken_seconds') or answer_row.get('time_taken_seconds') or answer_row.get('response_time') or 0,
                'answer_changed': row.get('answer_changed') if 'answer_changed' in row else answer_row.get('answer_changed', False),
                'difficulty': row.get('difficulty') or answer_row.get('difficulty') or 'Medium',
                'options': row.get('options') or {},
            })

        use_llm = bool(request.data.get('use_llm', False))
        analysis = analyze_test_submission(
            normalized_rows,
            student_name=request.data.get('student_name'),
            test_name=request.data.get('test_name'),
            subject_name=request.data.get('subject_name'),
            use_llm=use_llm,
        )
        return Response({'analysis': analysis, **analysis}, status=status.HTTP_200_OK)


class ValidateAIAnalysisOutputView(APIView):
    """
    POST /api/answer-script/validate-analysis/

    Body:
    {
        "questions": [
            {
                "question_id": 1,
                "question_text": "...",
                "topic": "...",
                "expected_concepts": ["...", "..."]
            }
        ],
        "student_answers": [
            {"question_id": 1, "answer_text": "..."}
        ],
        "ai_output": "<AI generated analysis to validate>"
    }
    """

    def post(self, request):
        questions = request.data.get('questions', [])
        ai_output = request.data.get('ai_output', '')

        if not isinstance(questions, list) or not questions:
            return Response(
                {'error': 'questions must be a non-empty list.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if isinstance(ai_output, str):
            ai_output = ai_output.strip()
            if not ai_output:
                return Response(
                    {'error': 'ai_output must be a non-empty string or object.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                parsed_output = json.loads(ai_output)
            except Exception:
                parsed_output = {'analysis': ai_output}
        elif isinstance(ai_output, dict):
            parsed_output = ai_output
        else:
            return Response(
                {'error': 'ai_output must be a non-empty string or object.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        report = parsed_output.get('analysis') if isinstance(parsed_output.get('analysis'), dict) else parsed_output
        validation = validate_analysis_report(report if isinstance(report, dict) else {})
        return Response({'report': validation}, status=status.HTTP_200_OK)


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
        now = timezone.now()
        UpcomingTest.objects.filter(status='scheduled', start_time__lte=now).update(status='active')
        UpcomingTest.objects.exclude(status='finished').filter(end_time__isnull=False, end_time__lt=now).update(status='finished')

        queryset = UpcomingTest.objects.all()
        class_name = self.request.query_params.get('class_name')
        assigned_class = self.request.query_params.get('assigned_class')
        student_id = self.request.query_params.get('student_id')

        if student_id:
            try:
                student = Student.objects.get(pk=student_id)
                queryset = queryset.filter(class_name=student.class_name, questions_generated=True)
            except Student.DoesNotExist:
                return UpcomingTest.objects.none()
        elif assigned_class and assigned_class != 'Class N/A':
            # Filter by teacher's assigned class
            queryset = queryset.filter(class_name=assigned_class)
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

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        student_id = request.query_params.get('student_id')

        if not student_id:
            return response

        try:
            sid = int(student_id)
        except (TypeError, ValueError):
            return response

        payload = response.data if isinstance(response.data, list) else []
        student = Student.objects.filter(pk=sid).first()


        submitted_ids = set()
        now = timezone.now()
        if student:
            test_ids = [row.get('id') for row in payload if row.get('id')]
            test_map = {
                test.id: test
                for test in UpcomingTest.objects.filter(id__in=test_ids)
            }

            # Auto-finalize only ended tests that have not been submitted yet.
            for test in test_map.values():
                if test.end_time and test.end_time < now:
                    _finalize_test_submission(test, student)

            # Prefer exact linkage via TestAttempt/TestResult to avoid false matches
            # when different tests share the same test_name/date.
            if _table_exists('tracker_testattempt'):
                submitted_ids.update(
                    TestAttempt.objects.filter(student=student, test_id__in=test_ids)
                    .values_list('test_id', flat=True)
                )

            if _table_exists('tracker_testresult'):
                submitted_ids.update(
                    TestResult.objects.filter(student=student, test_id__in=test_ids)
                    .exclude(status='InProgress')
                    .values_list('test_id', flat=True)
                )

            # Legacy fallback only when no test-linked submission tables are available.
            if not _table_exists('tracker_testattempt') and not _table_exists('tracker_testresult'):
                for tid, test in test_map.items():
                    if _student_has_submitted_test(student, test):
                        submitted_ids.add(tid)

        for row in payload:
            test_id = row.get('id')
            already_submitted = test_id in submitted_ids
            row['already_submitted'] = already_submitted
            try:
                start_dt = _coerce_aware_datetime(row.get('start_time'))
            except Exception:
                start_dt = None
            try:
                end_dt = _coerce_aware_datetime(row.get('end_time'))
            except Exception:
                end_dt = None

            # Mandatory debug trace for time-window classification.
            logger.warning(
                'TEST_STATUS_DEBUG test_id=%s current_time=%s start_time=%s end_time=%s',
                test_id,
                now.isoformat(),
                start_dt.isoformat() if start_dt else None,
                end_dt.isoformat() if end_dt else None,
            )

            # Time-only classification rule:
            # now < start => upcoming
            # start <= now <= end => active
            # now > end => past
            if start_dt and now < start_dt:
                time_status = 'scheduled'
            elif start_dt and end_dt and start_dt <= now <= end_dt:
                time_status = 'active'
            elif end_dt and now > end_dt:
                time_status = 'finished'
            elif start_dt and now >= start_dt:
                time_status = 'active'
            else:
                time_status = 'scheduled'

            has_started = bool(start_dt and now >= start_dt)
            has_ended = bool(end_dt and now > end_dt)

            row['status'] = 'finished' if already_submitted else time_status

            row['is_available_now'] = bool(has_started and not has_ended and not already_submitted)
            row['is_past'] = bool(already_submitted or has_ended)
            row['is_upcoming'] = bool(not row['is_past'])
            row['workflow_status'] = _workflow_status(type('T', (), {
                'status': row.get('status'),
                'questions_generated': row.get('questions_generated', False),
            })())

        return response


class UpcomingTestRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    queryset = UpcomingTest.objects.all()
    serializer_class = UpcomingTestSerializer

    def get_object(self):
        instance = super().get_object()
        if instance.status == 'scheduled' and instance.start_time and instance.start_time <= timezone.now():
            instance.status = 'active'
            instance.save(update_fields=['status'])
        if instance.status != 'finished' and instance.end_time and instance.end_time < timezone.now():
            instance.status = 'finished'
            instance.save(update_fields=['status'])
        return instance

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != 'scheduled':
            return Response(
                {'error': 'Test cannot be modified once it becomes active.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        # Edge-case testing mode: allow deleting tests from DB regardless of status.
        self.get_object()
        return super().destroy(request, *args, **kwargs)


class TeacherQuestionsReviewView(APIView):
    """
    GET /api/upcoming-tests/<id>/teacher-questions-review/

    Returns the manually authored MCQ set saved for a scheduled test.
    """

    def get(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)
        teacher_id = request.query_params.get('teacher_id')

        # Optional access control if test is teacher-bound.
        if test.teacher_id and teacher_id and str(test.teacher_id) != str(teacher_id):
            return Response({'error': 'You are not authorized to view this test.'}, status=status.HTTP_403_FORBIDDEN)

        questions = test.question_bank if isinstance(test.question_bank, list) else []
        return Response({
            'test_id': test.id,
            'test_name': test.test_name,
            'questions': questions,
            'num_questions': test.num_questions,
            'total_marks': test.total_marks,
            'workflow_status': _workflow_status(test),
            'is_published': bool(test.questions_generated),
            'is_editable': (not test.questions_generated) and test.status == 'scheduled',
        }, status=status.HTTP_200_OK)


class DraftTeacherQuestionsView(APIView):
    """
    POST /api/upcoming-tests/<id>/draft-questions/

    Saves draft questions without publishing to students.
    """

    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)

        if test.status != 'scheduled':
            return Response({'error': 'Draft can only be updated before test starts.'}, status=status.HTTP_400_BAD_REQUEST)

        if test.questions_generated:
            return Response({'error': 'This test is already published. Draft edits are locked.'}, status=status.HTTP_400_BAD_REQUEST)

        teacher_id = request.data.get('teacher_id')
        questions = request.data.get('questions', [])

        if test.teacher_id and teacher_id and str(test.teacher_id) != str(teacher_id):
            return Response({'error': 'You are not authorized to update this test.'}, status=status.HTTP_403_FORBIDDEN)

        if not isinstance(questions, list) or not questions:
            return Response({'error': 'At least one question is required.'}, status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        for idx, row in enumerate(questions, start=1):
            q_text = str(row.get('question_text', '')).strip()
            options = row.get('options') or {}
            correct = str(row.get('correct_answer', '')).strip()
            topic = str(row.get('topic', test.subject or test.topic or 'General')).strip()
            difficulty = str(row.get('difficulty', 'Medium')).strip() or 'Medium'

            if not q_text:
                return Response({'error': f'Question {idx}: question_text is required.'}, status=status.HTTP_400_BAD_REQUEST)
            if not isinstance(options, dict) or len(options) < 2:
                return Response({'error': f'Question {idx}: at least 2 options are required.'}, status=status.HTTP_400_BAD_REQUEST)

            normalized_options = {}
            for key, value in options.items():
                k = str(key).strip()
                v = str(value).strip()
                if not k or not v:
                    return Response({'error': f'Question {idx}: option keys and values cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
                normalized_options[k] = v

            if correct not in normalized_options:
                return Response({'error': f'Question {idx}: correct_answer must match an option key.'}, status=status.HTTP_400_BAD_REQUEST)

            cleaned.append({
                'question_text': q_text,
                'question_type': 'MCQ',
                'options': normalized_options,
                'correct_answer': correct,
                'marks': 1,
                'topic': topic,
                'difficulty': difficulty,
            })

        test.question_bank = cleaned
        test.num_questions = len(cleaned)
        test.total_marks = len(cleaned)
        test.questions_generated = False
        test.save(update_fields=['question_bank', 'num_questions', 'total_marks', 'questions_generated'])

        return Response({'message': 'Draft saved successfully.', 'saved': len(cleaned)}, status=status.HTTP_200_OK)


class PublishTeacherQuestionsView(APIView):
    """
    POST /api/upcoming-tests/<id>/publish-questions/

    Body:
    {
      "teacher_id": 4,
      "questions": [
        {
          "question_text": "...",
          "question_type": "MCQ",
          "options": {"A": "...", "B": "..."},
          "correct_answer": "A",
          "marks": 2
        }
      ]
    }
    """

    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)

        if test.status == 'scheduled' and test.start_time and test.start_time <= timezone.now():
            test.status = 'active'
            test.save(update_fields=['status'])

        if test.questions_generated:
            return Response({'error': 'Test is already published. Questions are locked.'}, status=status.HTTP_400_BAD_REQUEST)

        teacher_id = request.data.get('teacher_id')
        questions = request.data.get('questions', [])

        if test.teacher_id and teacher_id and str(test.teacher_id) != str(teacher_id):
            return Response({'error': 'You are not authorized to update this test.'}, status=status.HTTP_403_FORBIDDEN)

        if not isinstance(questions, list) or not questions:
            return Response({'error': 'At least one question is required.'}, status=status.HTTP_400_BAD_REQUEST)

        cleaned = []

        for idx, row in enumerate(questions, start=1):
            q_text = str(row.get('question_text', '')).strip()
            q_type = str(row.get('question_type', 'MCQ')).strip() or 'MCQ'
            options = row.get('options') or {}
            correct = str(row.get('correct_answer', '')).strip()
            topic = str(row.get('topic', test.subject or test.topic or 'General')).strip() or (test.subject or test.topic or 'General')
            difficulty = str(row.get('difficulty', 'medium')).strip().lower() or 'medium'

            if not q_text:
                return Response({'error': f'Question {idx}: question_text is required.'}, status=status.HTTP_400_BAD_REQUEST)

            if q_type != 'MCQ':
                return Response({'error': f'Question {idx}: only MCQ questions are supported.'}, status=status.HTTP_400_BAD_REQUEST)

            if not isinstance(options, dict) or len(options) < 2:
                return Response({'error': f'Question {idx}: at least 2 options are required.'}, status=status.HTTP_400_BAD_REQUEST)

            normalized_options = {}
            for key, value in options.items():
                k = str(key).strip()
                v = str(value).strip()
                if not k or not v:
                    return Response({'error': f'Question {idx}: option keys and values cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
                normalized_options[k] = v

            if correct not in normalized_options:
                return Response({'error': f'Question {idx}: correct_answer must match an option key.'}, status=status.HTTP_400_BAD_REQUEST)

            if difficulty not in ('easy', 'medium', 'hard'):
                return Response({'error': f'Question {idx}: difficulty must be easy, medium, or hard.'}, status=status.HTTP_400_BAD_REQUEST)

            cleaned.append({
                'question_text': q_text,
                'question_type': 'MCQ',
                'options': normalized_options,
                'correct_answer': correct,
                'marks': 1,
                'topic': topic,
                'difficulty': difficulty,
            })

        test.question_bank = cleaned
        test.num_questions = len(cleaned)
        test.total_marks = len(cleaned)
        test.questions_generated = True
        test.save(update_fields=['question_bank', 'num_questions', 'total_marks', 'questions_generated'])

        return Response(
            {
                'message': 'Questions published successfully.',
                'saved': len(cleaned),
                'num_questions': test.num_questions,
                'total_marks': test.total_marks,
                'workflow_status': 'Published',
            },
            status=status.HTTP_200_OK,
        )


class ParsePastedTeacherQuestionsView(APIView):
    """
    POST /api/upcoming-tests/<id>/parse-pasted-questions/

    Body:
    {
      "teacher_id": 4,
      "text": "1) Question...?\nA) ...\nB) ...\nC) ...\nD) ...\nAnswer: B"
    }
    """

    _q_start = re.compile(r'^\s*(?:q(?:uestion)?\s*\d+|\d+)[\).:\-\s]+(.+)$', re.IGNORECASE)
    _opt_line = re.compile(r'^\s*([A-D])[\).:\-\s]+(.+)$', re.IGNORECASE)
    _answer_line = re.compile(r'^\s*(?:answer|ans)\s*[:\-]?\s*([A-D])\s*$', re.IGNORECASE)

    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)

        teacher_id = request.data.get('teacher_id')
        if test.teacher_id and teacher_id and str(test.teacher_id) != str(teacher_id):
            return Response({'error': 'You are not authorized to parse questions for this test.'}, status=status.HTTP_403_FORBIDDEN)

        raw_text = str(request.data.get('text', '') or '').strip()
        default_topic = str(request.data.get('topic', test.subject or test.topic or 'General')).strip() or (test.subject or test.topic or 'General')
        default_difficulty = str(request.data.get('difficulty', 'medium')).strip().lower() or 'medium'

        if not raw_text:
            return Response({'error': 'text is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if default_difficulty not in ('easy', 'medium', 'hard'):
            return Response({'error': 'difficulty must be easy, medium, or hard.'}, status=status.HTTP_400_BAD_REQUEST)

        lines = [line.rstrip() for line in raw_text.splitlines()]
        blocks = []
        current = []

        for line in lines:
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)

        parsed = []
        rejected = []

        for index, block in enumerate(blocks, start=1):
            question_text = ''
            options = {}
            detected_answer = ''

            for line in block:
                line_text = line.strip()
                ans_match = self._answer_line.match(line_text)
                if ans_match:
                    detected_answer = ans_match.group(1).upper()
                    continue

                opt_match = self._opt_line.match(line_text)
                if opt_match:
                    key = opt_match.group(1).upper()
                    value = opt_match.group(2).strip()
                    if value:
                        options[key] = value
                    continue

                q_match = self._q_start.match(line_text)
                if q_match and not question_text:
                    question_text = q_match.group(1).strip()
                    continue

                if not question_text:
                    question_text = line_text
                else:
                    question_text = f"{question_text} {line_text}".strip()

            if len(options) < 2 or not question_text:
                rejected.append({
                    'block': index,
                    'reason': 'Could not detect a valid MCQ question with at least 2 options.',
                    'raw': '\n'.join(block),
                })
                continue

            option_keys = sorted(options.keys())
            if detected_answer and detected_answer not in options:
                rejected.append({
                    'block': index,
                    'reason': f'Detected answer {detected_answer} is not present in options {option_keys}.',
                    'raw': '\n'.join(block),
                })
                continue

            correct_answer = detected_answer or option_keys[0]

            parsed.append({
                'question_text': question_text,
                'question_type': 'MCQ',
                'options': options,
                'correct_answer': correct_answer,
                'marks': 1,
                'topic': default_topic,
                'difficulty': default_difficulty,
            })

        return Response(
            {
                'parsed_count': len(parsed),
                'rejected_count': len(rejected),
                'questions': parsed,
                'rejected_blocks': rejected,
                'next_step': 'Send returned questions to /draft-questions/ or /publish-questions/.' if parsed else 'Fix rejected blocks and try again.',
            },
            status=status.HTTP_200_OK,
        )


class UploadTeacherQuestionsCSVView(APIView):
    """
    POST /api/upcoming-tests/<id>/upload-questions-csv/

    Form-data:
    - teacher_id: optional (required when test has bound teacher)
    - file: csv file
    - publish: optional boolean, default false

    Supported CSV headers (case-insensitive):
    - question_text
    - option_a
    - option_b
    - option_c
    - option_d
    - correct_answer
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)

        teacher_id = request.data.get('teacher_id')
        if test.teacher_id and teacher_id and str(test.teacher_id) != str(teacher_id):
            return Response({'error': 'You are not authorized to upload questions for this test.'}, status=status.HTTP_403_FORBIDDEN)

        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': 'CSV file is required under field name "file".'}, status=status.HTTP_400_BAD_REQUEST)

        fallback_topic = str(test.subject or test.topic or 'General').strip() or 'General'
        fallback_difficulty = 'medium'

        publish_flag = str(request.data.get('publish', 'false')).strip().lower() in {'1', 'true', 'yes', 'on'}

        try:
            raw_bytes = upload.read()
            try:
                decoded = raw_bytes.decode('utf-8-sig')
            except UnicodeDecodeError:
                decoded = raw_bytes.decode('latin-1')
        except Exception:
            return Response({'error': 'Unable to read uploaded CSV file.'}, status=status.HTTP_400_BAD_REQUEST)

        if not str(decoded or '').strip():
            return Response({'error': 'File is empty'}, status=status.HTTP_400_BAD_REQUEST)

        reader = csv.DictReader(io.StringIO(decoded))
        headers = [str(h).strip().lower() for h in (reader.fieldnames or []) if str(h).strip()]
        required_headers = ['question_text', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_answer']
        if not headers:
            return Response({'error': 'Invalid CSV format'}, status=status.HTTP_400_BAD_REQUEST)

        if sorted(headers) != sorted(required_headers):
            return Response({'error': 'Invalid CSV format'}, status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        rejected = []
        seen_questions = set()

        for idx, row in enumerate(reader, start=2):
            question_text = str(row.get('question_text', '') or '').strip()
            opt_a = str(row.get('option_a', '') or '').strip()
            opt_b = str(row.get('option_b', '') or '').strip()
            opt_c = str(row.get('option_c', '') or '').strip()
            opt_d = str(row.get('option_d', '') or '').strip()
            correct = str(row.get('correct_answer', '') or '').strip().upper()
            topic = fallback_topic
            difficulty = fallback_difficulty

            options = {}
            if opt_a:
                options['A'] = opt_a
            if opt_b:
                options['B'] = opt_b
            if opt_c:
                options['C'] = opt_c
            if opt_d:
                options['D'] = opt_d

            if not question_text:
                rejected.append({'row': idx, 'reason': 'Question text is empty'})
                continue
            if len(options) < 2:
                rejected.append({'row': idx, 'reason': f'At least 2 options required at row {idx}'})
                continue
            if correct not in options:
                rejected.append({'row': idx, 'reason': f'Correct answer mismatch at row {idx}'})
                continue

            normalized_question = ' '.join(question_text.lower().split())
            normalized_options = tuple((key, ' '.join(value.lower().split())) for key, value in sorted(options.items()))
            duplicate_key = (normalized_question, normalized_options, correct)
            if duplicate_key in seen_questions:
                rejected.append({'row': idx, 'reason': f'Duplicate question skipped at row {idx}'})
                continue
            seen_questions.add(duplicate_key)

            cleaned.append({
                'question_text': question_text,
                'question_type': 'MCQ',
                'options': options,
                'correct_answer': correct,
                'marks': 1,
                'topic': topic,
                'difficulty': difficulty,
            })

        if not cleaned:
            return Response(
                {
                    'error': 'No valid questions found in CSV.',
                    'parsed_count': 0,
                    'rejected_count': len(rejected),
                    'rejected_rows': rejected,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        test.question_bank = cleaned
        test.num_questions = len(cleaned)
        test.total_marks = len(cleaned)
        test.questions_generated = bool(publish_flag)
        test.save(update_fields=['question_bank', 'num_questions', 'total_marks', 'questions_generated'])

        return Response(
            {
                'message': 'CSV questions imported successfully.',
                'saved': len(cleaned),
                'rejected_count': len(rejected),
                'rejected_rows': rejected,
                'num_questions': test.num_questions,
                'total_marks': test.total_marks,
                'workflow_status': 'Published' if test.questions_generated else 'Draft',
            },
            status=status.HTTP_200_OK,
        )


def _coerce_question_bank(test):
    bank = test.question_bank if isinstance(test.question_bank, list) else []
    cleaned = []
    for idx, row in enumerate(bank, start=1):
        options = row.get('options') if isinstance(row, dict) else {}
        if not isinstance(options, dict):
            options = {}
        cleaned.append({
            'id': idx,
            'question_text': str(row.get('question_text', '')).strip() if isinstance(row, dict) else '',
            'question_type': 'MCQ',
            'options': options,
            'correct_answer': str(row.get('correct_answer', '')).strip() if isinstance(row, dict) else '',
            'marks': 1,
            'topic': str(row.get('topic', test.subject or test.topic or 'General')).strip() if isinstance(row, dict) else (test.subject or test.topic or 'General'),
            'difficulty': str(row.get('difficulty', 'Medium')).strip() if isinstance(row, dict) else 'Medium',
        })
    return cleaned


def _runtime_cache_key(student_id, test_id):
    return f"runtime_answers:{student_id}:{test_id}"


def _table_columns(table_name):
    """Get column names for a table (database-agnostic)."""
    try:
        from django.db import connection
        inspector = connection.introspection
        cursor = connection.cursor()
        
        # Check if table exists
        tables = [t[0] for t in inspector.get_table_list(cursor)]
        if table_name not in tables:
            return set()
        
        # Get field information
        field_info = inspector.get_columns(cursor, table_name)
        return {field[0] for field in field_info}
    except Exception:
        return set()


def _table_exists(table_name):
    return bool(_table_columns(table_name))


def _persisted_answers_map(student, test):
    answers_map = {}
    columns = _table_columns('tracker_studenttestresponse')
    if not columns:
        return answers_map

    try:
        with connection.cursor() as cursor:
            if {'selected_answer', 'time_taken_seconds', 'answer_changed', 'question_id'}.issubset(columns):
                cursor.execute(
                    """
                    SELECT question_id, COALESCE(selected_answer, ''), COALESCE(time_taken_seconds, 0), COALESCE(answer_changed, 0)
                    FROM tracker_studenttestresponse
                    WHERE student_id = %s AND test_id = %s
                    ORDER BY question_id
                    """,
                    [student.id, test.id],
                )
                rows = cursor.fetchall()
                for row in rows:
                    if not row or len(row) < 4:
                        continue
                    answers_map[str(int(row[0]))] = {
                        'answer': str(row[1] or '').strip(),
                        'time_taken_seconds': max(0, int(row[2] or 0)),
                        'answer_changed': bool(row[3]),
                    }
            elif {'student_answer', 'response_time', 'question_id'}.issubset(columns):
                cursor.execute(
                    """
                    SELECT COALESCE(student_answer, ''), COALESCE(response_time, 0)
                    FROM tracker_studenttestresponse
                    WHERE student_id = %s AND test_id = %s
                    ORDER BY question_id, id
                    """,
                    [student.id, test.id],
                )
                rows = cursor.fetchall()
                for idx, row in enumerate(rows, start=1):
                    if not row or len(row) < 2:
                        continue
                    answers_map[str(idx)] = {
                        'answer': str(row[0] or '').strip(),
                        'time_taken_seconds': max(0, int(row[1] or 0)),
                        'answer_changed': False,
                    }
    except Exception:
        return {}

    return answers_map


def _attempt_answers_map(student, test):
    if not _table_exists('tracker_testattempt'):
        return {}
    attempt = TestAttempt.objects.filter(student=student, test=test).order_by('-updated_at').first()
    if not attempt:
        return {}

    payload = attempt.answers_payload if isinstance(attempt.answers_payload, list) else []
    answers_map = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        qid = row.get('question_id')
        if qid is None or qid == '':
            continue
        qid_str = str(qid)
        answers_map[qid_str] = {
            'answer': str(row.get('selected_answer', '') or '').strip(),
            'time_taken_seconds': max(0, int(row.get('time_taken_seconds', 0) or 0)),
            'answer_changed': bool(row.get('answer_changed', False)),
        }
    return answers_map


def _resolved_answers_map(student, test):
    runtime = cache.get(_runtime_cache_key(student.id, test.id), {})
    if isinstance(runtime, dict) and runtime:
        return runtime
    attempt_answers = _attempt_answers_map(student, test)
    if attempt_answers:
        return attempt_answers
    return _persisted_answers_map(student, test)


def _question_level_rows(test, student):
    question_bank = _coerce_question_bank(test)
    answers_map = _resolved_answers_map(student, test)
    rows = []

    for question in question_bank:
        question_id = int(question['id'])
        entry = answers_map.get(str(question_id), {}) if isinstance(answers_map, dict) else {}
        selected_answer = str(entry.get('answer', '') or '').strip()
        try:
            time_taken_seconds = max(0, int(entry.get('time_taken_seconds', 0) or 0))
        except (TypeError, ValueError):
            time_taken_seconds = 0

        rows.append({
            'question_id': question_id,
            'question_text': question.get('question_text', ''),
            'question_type': question.get('question_type', 'MCQ'),
            'selected_answer': selected_answer,
            'correct_answer': str(question.get('correct_answer', '') or '').strip(),
            'topic': question.get('topic', '') or 'General',
            'is_correct': bool(selected_answer) and selected_answer == str(question.get('correct_answer', '') or '').strip(),
            'time_taken_seconds': time_taken_seconds,
            'answer_changed': bool(entry.get('answer_changed', False)),
            'difficulty': str(question.get('difficulty', 'medium') or 'medium').lower(),
            'options': question.get('options', {}),
        })

    return rows


def _attempt_summary(student, test):
    if not _table_exists('tracker_testattempt'):
        return None
    return TestAttempt.objects.filter(student=student, test=test).order_by('-updated_at').first()


def _pattern_message(default_message, patterns):
    if isinstance(patterns, list) and patterns:
        return patterns
    return [default_message]


def _strength_message(default_message, strengths):
    if isinstance(strengths, list) and strengths:
        return strengths
    return [default_message]


def _analyze_question_strengths(question_rows):
    if not question_rows:
        return []

    topic_stats = {}
    total_correct = 0
    total_questions = len(question_rows)

    for row in question_rows:
        topic = str(row.get('topic') or 'General').strip() or 'General'
        topic_stats.setdefault(topic, {'total': 0, 'correct': 0, 'examples': []})
        topic_stats[topic]['total'] += 1
        if row.get('is_correct'):
            topic_stats[topic]['correct'] += 1
            total_correct += 1
            if len(topic_stats[topic]['examples']) < 2:
                topic_stats[topic]['examples'].append(str(row.get('question_text', '')).strip())

    strengths = []
    strong_topics = []

    for topic, stats in topic_stats.items():
        total = int(stats.get('total', 0))
        correct = int(stats.get('correct', 0))
        pct = round((correct / total) * 100) if total else 0
        if total >= 2 and pct >= 75:
            strong_topics.append((topic, pct, stats.get('examples', [])))

    strong_topics.sort(key=lambda item: (item[1], item[0]), reverse=True)

    if total_questions and total_correct == total_questions:
        strengths.append(
            "- You answered the full script correctly, which shows solid conceptual coverage across the tested topics."
        )
        best_topic = max(topic_stats.items(), key=lambda item: (item[1]['correct'], item[1]['total']))[0]
        strengths.append(
            f"- {best_topic} looks particularly well established because every question from that topic was handled correctly."
        )
        return strengths

    if strong_topics:
        for topic, pct, examples in strong_topics[:4]:
            example_text = f" Examples: {', '.join(examples)}." if examples else ""
            strengths.append(
                f"- {topic} is a clear strength because most questions from this topic were answered correctly ({pct}% correct).{example_text}"
            )

    high_confidence_topics = [topic for topic, stats in topic_stats.items() if int(stats.get('correct', 0)) >= 2 and int(stats.get('correct', 0)) >= int(stats.get('total', 0)) - 1]
    for topic in high_confidence_topics:
        if all(topic not in line for line in strengths):
            strengths.append(
                f"- {topic} appears well controlled: the answers from this area show consistent concept recall and few misses."
            )

    return strengths


def _build_comparison(current_entry, previous_entry):
    if not previous_entry:
        return {
            'status': 'first_test',
            'title': 'First recorded test',
            'summary': 'This is the first recorded test in this subject, so comparison will appear after the next test.',
            'previous_test_name': None,
            'previous_test_date': None,
            'previous_score': None,
            'score_change': None,
        }

    score_change = int(current_entry['percentage']) - int(previous_entry['percentage'])
    if score_change > 0:
        title = 'Improved from previous test'
        summary = f"You improved compared with {previous_entry['test_name']}."
        status = 'improved'
    elif score_change < 0:
        title = 'Dropped from previous test'
        summary = f"Your performance dropped compared with {previous_entry['test_name']}."
        status = 'declined'
    else:
        title = 'Similar to previous test'
        summary = f"Your performance stayed at a similar level compared with {previous_entry['test_name']}."
        status = 'stable'

    return {
        'status': status,
        'title': title,
        'summary': summary,
        'previous_test_name': previous_entry['test_name'],
        'previous_test_date': previous_entry['test_date'],
        'previous_score': previous_entry['percentage'],
        'score_change': score_change,
    }


def _legacy_attempt_defaults(student, test, mark, question_rows, conceptual_patterns, behavior_patterns):
    correct_count = sum(1 for row in question_rows if row.get('is_correct'))
    attempted_count = sum(1 for row in question_rows if row.get('selected_answer'))
    unattempted_count = max(0, len(question_rows) - attempted_count)
    accuracy = round((correct_count / attempted_count) * 100, 2) if attempted_count else 0
    attempt_rate = round((attempted_count / len(question_rows)) * 100, 2) if question_rows else 0
    time_taken_seconds = sum(int(row.get('time_taken_seconds', 0) or 0) for row in question_rows)

    return {
        'answers_payload': question_rows,
        'conceptual_patterns': conceptual_patterns,
        'behavior_patterns': behavior_patterns,
        'score': float(mark.marks_obtained or 0),
        'total_marks': float(mark.total_marks or 0),
        'correct_count': correct_count,
        'incorrect_count': max(0, attempted_count - correct_count),
        'unattempted_count': unattempted_count,
        'attempted_count': attempted_count,
        'accuracy': accuracy,
        'attempt_rate': attempt_rate,
        'time_taken_seconds': time_taken_seconds,
    }


def _compute_response_stats(test, student, answers_map=None):
    question_bank = _coerce_question_bank(test)
    total_questions = len(question_bank)
    total_marks = total_questions if total_questions else int(test.total_marks or 0)

    runtime = answers_map if isinstance(answers_map, dict) else cache.get(_runtime_cache_key(student.id, test.id), {})

    correct_count = 0
    attempted_count = 0
    time_taken_seconds = 0

    for idx, q in enumerate(question_bank, start=1):
        entry = runtime.get(str(idx), {}) if isinstance(runtime, dict) else {}
        ans = str(entry.get('answer', '')).strip()
        if ans:
            attempted_count += 1
            if ans == str(q.get('correct_answer', '')).strip():
                correct_count += 1
        try:
            time_taken_seconds += max(0, int(entry.get('time_taken_seconds', 0) or 0))
        except (TypeError, ValueError):
            pass

    incorrect_count = max(0, attempted_count - correct_count)
    unattempted_count = max(0, total_questions - attempted_count)
    score = float(correct_count)
    accuracy = round((correct_count / attempted_count) * 100, 2) if attempted_count else 0
    attempt_rate = round((attempted_count / total_questions) * 100, 2) if total_questions else 0

    topic_wise = {}
    for q in question_bank:
        topic = q.get('topic') or 'General'
        if topic not in topic_wise:
            topic_wise[topic] = {'total': 0, 'correct': 0}
        topic_wise[topic]['total'] += 1

    for idx, q in enumerate(question_bank, start=1):
        entry = runtime.get(str(idx), {}) if isinstance(runtime, dict) else {}
        ans = str(entry.get('answer', '')).strip()
        topic = q.get('topic') or 'General'
        if ans and ans == str(q.get('correct_answer', '')).strip():
            if topic not in topic_wise:
                topic_wise[topic] = {'total': 0, 'correct': 0}
            topic_wise[topic]['correct'] += 1

    topic_wise_analysis = {}
    for topic, stats in topic_wise.items():
        total = int(stats.get('total', 0))
        corr = int(stats.get('correct', 0))
        pct = round((corr / total) * 100, 2) if total else 0
        topic_wise_analysis[topic] = {'total': total, 'correct': corr, 'percentage': pct}

    return {
        'total_questions': total_questions,
        'total_marks': total_marks,
        'score': score,
        'correct_count': correct_count,
        'incorrect_count': incorrect_count,
        'unattempted_count': unattempted_count,
        'attempted_count': attempted_count,
        'accuracy': accuracy,
        'attempt_rate': attempt_rate,
        'time_taken_seconds': time_taken_seconds,
        'topic_wise_analysis': topic_wise_analysis,
    }


def _finalize_test_submission(test, student, runtime=None):
    key = _runtime_cache_key(student.id, test.id)
    payload = runtime if isinstance(runtime, dict) else cache.get(key, {})
    if not isinstance(payload, dict) or not payload:
        return None

    cache.set(key, payload, timeout=60 * 60 * 6)

    stats = _compute_response_stats(test, student, payload)
    question_rows = _question_level_rows(test, student)
    analysis = analyze_test_submission(
        _question_level_rows(test, student),
        student_name=student.name,
        test_name=test.test_name,
        subject_name=test.subject or test.topic or 'General',
        use_llm=False,
    )
    conceptual_patterns = analysis.get('conceptual_patterns', [])
    behavior_patterns = analysis.get('behavior_patterns', [])
    strengths = analysis.get('strengths', [])
    weaknesses = analysis.get('weaknesses', [])
    recommendations = analysis.get('recommendations', [])

    question_bank = _coerce_question_bank(test)
    for question in question_bank:
        question_id = int(question['id'])
        entry = payload.get(str(question_id), {}) if isinstance(payload, dict) else {}
        selected_answer = str(entry.get('answer', '') or '').strip()
        try:
            time_taken_seconds = max(0, int(entry.get('time_taken_seconds', 0) or 0))
        except (TypeError, ValueError):
            time_taken_seconds = 0
        answer_changed = bool(entry.get('answer_changed', False))
        correct_answer = str(question.get('correct_answer', '') or '').strip()
        is_correct = bool(selected_answer) and selected_answer == correct_answer

        try:
            StudentTestResponse.objects.update_or_create(
                student=student,
                test=test,
                question_id=question_id,
                defaults={
                    'question_text': question.get('question_text', ''),
                    'selected_answer': selected_answer,
                    'correct_answer': correct_answer,
                    'is_correct': is_correct,
                    'answer_changed': answer_changed,
                    'question_difficulty': str(question.get('difficulty', 'Medium') or 'Medium'),
                    'topic': str(question.get('topic', 'General') or 'General'),
                    'time_taken_seconds': time_taken_seconds,
                    'marks_awarded': 1.0 if is_correct else 0.0,
                },
            )
        except Exception as exc:
            logger.warning(
                'StudentTestResponse write skipped for student_id=%s test_id=%s question_id=%s error=%s',
                student.id,
                test.id,
                question_id,
                str(exc),
            )

    if _table_exists('tracker_testattempt'):
        TestAttempt.objects.update_or_create(
            student=student,
            test=test,
            defaults={
                'answers_payload': question_rows,
                'conceptual_patterns': conceptual_patterns,
                'behavior_patterns': behavior_patterns,
                'score': stats['score'],
                'total_marks': stats['total_marks'],
                'correct_count': stats['correct_count'],
                'incorrect_count': stats['incorrect_count'],
                'unattempted_count': stats['unattempted_count'],
                'attempted_count': stats['attempted_count'],
                'accuracy': stats['accuracy'],
                'attempt_rate': stats['attempt_rate'],
                'time_taken_seconds': stats['time_taken_seconds'],
            },
        )

    try:
        TestResult.objects.update_or_create(
            student=student,
            test=test,
            defaults={
                'total_score': stats['score'],
                'total_marks': stats['total_marks'],
                'percentage': round((stats['score'] / stats['total_marks']) * 100, 2) if stats['total_marks'] else 0,
                'status': 'Completed',
                'topic_wise_analysis': stats['topic_wise_analysis'],
                'strengths': strengths,
                'weaknesses': weaknesses,
                'recommendations': '\n'.join(f'- {item}' for item in recommendations),
                'predicted_performance': analysis.get('predicted_performance', {}),
            },
        )
    except Exception:
        pass

    subject_label = _subject_label(test.subject or test.topic or 'General')
    candidate_marks = TestMark.objects.filter(
        student=student,
        test_name=test.test_name,
        date_taken=test.test_date,
    ).order_by('-id')
    test_mark = next(
        (mark for mark in candidate_marks if _normalize_subject(mark.subject) == _normalize_subject(subject_label)),
        None,
    )

    if test_mark:
        test_mark.subject = subject_label
        test_mark.marks_obtained = stats['score']
        test_mark.total_marks = stats['total_questions'] or test.total_marks
        test_mark.save(update_fields=['subject', 'marks_obtained', 'total_marks'])
        created_mark = False
    else:
        test_mark = TestMark.objects.create(
            student=student,
            subject=subject_label,
            test_name=test.test_name,
            marks_obtained=stats['score'],
            total_marks=stats['total_questions'] or test.total_marks,
            date_taken=test.test_date,
        )
        created_mark = True

    logger.warning(
        'TEST_MARK_UPSERT_DEBUG student_id=%s test_id=%s subject=%s test_name=%s date=%s mark_id=%s created=%s',
        student.id,
        test.id,
        _normalize_subject(subject_label),
        ' '.join(str(test.test_name or '').strip().lower().split()),
        str(test.test_date),
        test_mark.id,
        created_mark,
    )

    _run_performance_analysis(test_mark)
    return stats


def _refresh_ai_analysis_async(test_id, student_id):
    if not bool(getattr(settings, 'APT_AI_BACKGROUND_ANALYSIS_ENABLED', True)):
        logger.info('Background AI analysis is disabled by configuration.')
        return

    inflight_key = f'ai-analysis:inflight:test:{test_id}:student:{student_id}'
    if not cache.add(inflight_key, '1', timeout=60 * 10):
        logger.info('Skipping duplicate background AI analysis job for test_id=%s student_id=%s', test_id, student_id)
        return

    def _worker():
        close_old_connections()
        try:
            student = Student.objects.filter(pk=student_id).first()
            test = UpcomingTest.objects.filter(pk=test_id).first()
            if not student or not test:
                return

            review_rows = _question_level_rows(test, student)
            if not review_rows:
                return

            analysis = analyze_test_submission(
                review_rows,
                student_name=student.name,
                test_name=test.test_name,
                subject_name=test.subject or test.topic or 'General',
                use_llm=True,
            )

            conceptual_patterns = analysis.get('conceptual_patterns', [])
            behavior_patterns = analysis.get('behavior_patterns', [])
            strengths = analysis.get('strengths', [])
            weaknesses = analysis.get('weaknesses', [])
            recommendations = analysis.get('recommendations', [])

            if _table_exists('tracker_testattempt'):
                TestAttempt.objects.filter(student=student, test=test).update(
                    conceptual_patterns=conceptual_patterns,
                    behavior_patterns=behavior_patterns,
                )

            TestResult.objects.filter(student=student, test=test).update(
                strengths=strengths,
                weaknesses=weaknesses,
                recommendations='\n'.join(f'- {item}' for item in recommendations),
                predicted_performance=analysis.get('predicted_performance', {}),
            )
        except Exception as exc:
            logger.warning(
                'ASYNC_AI_ANALYSIS_REFRESH_FAILED student_id=%s test_id=%s error=%s',
                student_id,
                test_id,
                str(exc),
            )
        finally:
            cache.delete(inflight_key)
            close_old_connections()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


class StudentTestDetailsView(APIView):
    def get(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)

        now = timezone.now()
        if test.status != 'finished' and test.end_time and test.end_time < now:
            test.status = 'finished'
            test.save(update_fields=['status'])

        student_id = request.query_params.get('student_id')
        student = Student.objects.filter(pk=student_id).first() if student_id else None
        if student and test.end_time and test.end_time < now:
            _finalize_test_submission(test, student)

        start_time = test.start_time
        end_time = test.end_time
        already_submitted = bool(student and _student_has_submitted_test(student, test))
        in_window = bool(start_time and end_time and start_time <= now <= end_time)

        study_material_url = test.study_material.url if test.study_material else None
        return Response({
            'id': test.id,
            'test_name': test.test_name,
            'subject': test.subject,
            'topic': test.topic,
            'test_date': test.test_date,
            'start_time': start_time,
            'end_time': end_time,
            'num_questions': test.num_questions,
            'total_marks': test.total_marks,
            'class_name': test.class_name,
            'status': test.status,
            'workflow_status': _workflow_status(test),
            'study_material_url': study_material_url,
            'already_submitted': already_submitted,
            'student_can_submit': (not already_submitted) and in_window,
        }, status=status.HTTP_200_OK)


class StudentTestQuestionsView(APIView):
    def get(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)
        question_bank = _coerce_question_bank(test)
        if not question_bank:
            return Response({'error': 'No questions available for this test yet.'}, status=status.HTTP_400_BAD_REQUEST)

        safe_questions = []
        for q in question_bank:
            safe_questions.append({
                'id': q['id'],
                'question_text': q['question_text'],
                'question_type': q['question_type'],
                'options': q['options'],
                'marks': q['marks'],
                'topic': q['topic'],
                'difficulty': q['difficulty'],
            })

        return Response({'questions': safe_questions}, status=status.HTTP_200_OK)


class StudentTestReviewView(APIView):
    def get(self, request, pk):
        student_id = request.query_params.get('student_id')
        if not student_id:
            return Response({'error': 'student_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        test = get_object_or_404(UpcomingTest, pk=pk)
        student = get_object_or_404(Student, pk=student_id)
        review_rows = _question_level_rows(test, student)
        answers_map = _resolved_answers_map(student, test)
        attempt = _attempt_summary(student, test)
        stats = _compute_response_stats(test, student, answers_map if answers_map else None)
        if attempt:
            stats.update({
                'score': float(attempt.score or 0),
                'total_marks': float(attempt.total_marks or 0),
                'correct_count': int(attempt.correct_count or 0),
                'incorrect_count': int(attempt.incorrect_count or 0),
                'unattempted_count': int(attempt.unattempted_count or 0),
                'attempted_count': int(attempt.attempted_count or 0),
                'accuracy': float(attempt.accuracy or 0),
                'attempt_rate': float(attempt.attempt_rate or 0),
                'time_taken_seconds': int(attempt.time_taken_seconds or 0),
            })
        percentage = round((stats['score'] / stats['total_marks']) * 100, 2) if stats['total_marks'] else 0
        analysis = analyze_test_submission(
            review_rows,
            student_name=student.name,
            test_name=test.test_name,
            subject_name=test.subject or test.topic or 'General',
            use_llm=False,
        )

        if attempt and isinstance(attempt.conceptual_patterns, list) and attempt.conceptual_patterns:
            analysis['conceptual_patterns'] = attempt.conceptual_patterns
        if attempt and isinstance(attempt.behavior_patterns, list) and attempt.behavior_patterns:
            analysis['behavior_patterns'] = attempt.behavior_patterns

        result_row = TestResult.objects.filter(student=student, test=test).order_by('-completed_at').first()
        if result_row:
            if isinstance(result_row.strengths, list) and result_row.strengths:
                analysis['strengths'] = result_row.strengths
            if isinstance(result_row.weaknesses, list) and result_row.weaknesses:
                analysis['weaknesses'] = result_row.weaknesses
            if isinstance(result_row.predicted_performance, dict) and result_row.predicted_performance:
                analysis['predicted_performance'] = result_row.predicted_performance

        return Response({
            'test_id': test.id,
            'test_name': test.test_name,
            'subject': test.subject or test.topic or 'General',
            'test_date': test.test_date,
            'score': stats['score'],
            'total_marks': stats['total_marks'],
            'percentage': percentage,
            'accuracy': stats['accuracy'],
            'attempt_rate': stats['attempt_rate'],
            'correct': stats['correct_count'],
            'incorrect': stats['incorrect_count'],
            'unattempted': stats['unattempted_count'],
            'time_taken_seconds': stats['time_taken_seconds'],
            'questions': [
                {
                    **row,
                    'is_attempted': bool(row.get('selected_answer')),
                }
                for row in review_rows
            ],
            **analysis,
            'analysis': analysis,
        }, status=status.HTTP_200_OK)


class StudentSingleResponseView(APIView):
    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)
        student_id = request.data.get('student_id')
        question_id = request.data.get('question_id')
        answer = str(request.data.get('answer', '')).strip()
        time_taken_seconds = request.data.get('time_taken_seconds', 0)
        answer_changed = bool(request.data.get('answer_changed', False))

        if not student_id or not question_id:
            return Response({'error': 'student_id and question_id are required.'}, status=status.HTTP_400_BAD_REQUEST)

        student = get_object_or_404(Student, pk=student_id)
        can_attempt, reason = _can_student_attempt_test(test, student, allow_expired_submission=True)
        if not can_attempt:
            return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

        try:
            question_id = int(question_id)
        except (TypeError, ValueError):
            return Response({'error': 'question_id must be numeric.'}, status=status.HTTP_400_BAD_REQUEST)

        bank = _coerce_question_bank(test)
        if question_id <= 0 or question_id > len(bank):
            return Response({'error': 'Invalid question_id.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            time_taken_seconds = max(0, int(time_taken_seconds or 0))
        except (TypeError, ValueError):
            time_taken_seconds = 0

        key = _runtime_cache_key(student.id, test.id)
        runtime = cache.get(key, {})
        if not isinstance(runtime, dict):
            runtime = {}

        prior = runtime.get(str(question_id), {})
        if prior and str(prior.get('answer', '')).strip() and str(prior.get('answer', '')).strip() != answer:
            answer_changed = True

        runtime[str(question_id)] = {
            'answer': answer,
            'time_taken_seconds': time_taken_seconds,
            'answer_changed': bool(answer_changed),
        }
        cache.set(key, runtime, timeout=60 * 60 * 6)

        return Response({
            'saved': True,
        }, status=status.HTTP_200_OK)


class StudentSubmitTestView(APIView):
    def post(self, request, pk):
        test = get_object_or_404(UpcomingTest, pk=pk)
        student_id = request.data.get('student_id')
        responses = request.data.get('responses', [])

        if not student_id:
            return Response({'error': 'student_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        student = get_object_or_404(Student, pk=student_id)
        can_attempt, reason = _can_student_attempt_test(test, student)
        if not can_attempt:
            return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

        key = _runtime_cache_key(student.id, test.id)
        runtime = cache.get(key, {})
        if not isinstance(runtime, dict):
            runtime = {}

        if isinstance(responses, list):
            for row in responses:
                qid = row.get('question_id')
                if not qid:
                    continue
                try:
                    qid_int = int(qid)
                except (TypeError, ValueError):
                    continue
                ans = str(row.get('answer', '') or '').strip()
                try:
                    tts = max(0, int(row.get('time_taken_seconds', 0) or 0))
                except (TypeError, ValueError):
                    tts = 0
                runtime[str(qid_int)] = {
                    'answer': ans,
                    'time_taken_seconds': tts,
                    'answer_changed': bool(row.get('answer_changed', False)),
                }

        stats = _finalize_test_submission(test, student, runtime)
        if not stats:
            return Response({'error': 'No responses available to submit.'}, status=status.HTTP_400_BAD_REQUEST)

        _refresh_ai_analysis_async(test.id, student.id)

        return Response({
            'score': stats['score'],
            'total_marks': stats['total_marks'],
            'accuracy': stats['accuracy'],
            'attempt_rate': stats['attempt_rate'],
            'correct': stats['correct_count'],
            'incorrect': stats['incorrect_count'],
            'unattempted': stats['unattempted_count'],
            'time_taken_seconds': stats['time_taken_seconds'],
            'status': 'Completed',
            'ai_analysis_status': 'processing',
        }, status=status.HTTP_200_OK)


class StudentTestResultView(APIView):
    def get(self, request, pk):
        student_id = request.query_params.get('student_id')
        if not student_id:
            return Response({'error': 'student_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        test = get_object_or_404(UpcomingTest, pk=pk)
        student = get_object_or_404(Student, pk=student_id)
        if test.end_time and test.end_time < timezone.now():
            _finalize_test_submission(test, student)
        key = _runtime_cache_key(student.id, test.id)
        runtime = cache.get(key, {})
        attempt = _attempt_summary(student, test)

        if isinstance(runtime, dict) and runtime:
            stats = _compute_response_stats(test, student, runtime)
        elif attempt:
            stats = {
                'score': float(attempt.score or 0),
                'total_marks': float(attempt.total_marks or 0),
                'accuracy': float(attempt.accuracy or 0),
                'attempt_rate': float(attempt.attempt_rate or 0),
                'correct_count': int(attempt.correct_count or 0),
                'incorrect_count': int(attempt.incorrect_count or 0),
                'unattempted_count': int(attempt.unattempted_count or 0),
                'time_taken_seconds': int(attempt.time_taken_seconds or 0),
            }
        else:
            persisted_answers = _persisted_answers_map(student, test)
            if persisted_answers:
                stats = _compute_response_stats(test, student, persisted_answers)
                return Response({
                    'score': stats['score'],
                    'total_marks': stats['total_marks'],
                    'accuracy': stats['accuracy'],
                    'attempt_rate': stats['attempt_rate'],
                    'correct': stats['correct_count'],
                    'incorrect': stats['incorrect_count'],
                    'unattempted': stats['unattempted_count'],
                    'time_taken_seconds': stats['time_taken_seconds'],
                    'status': 'Completed',
                }, status=status.HTTP_200_OK)

            expected_subject = _normalize_subject(test.subject or test.topic or 'General')
            candidate_marks = list(
                TestMark.objects.filter(
                    student=student,
                    test_name=test.test_name,
                    date_taken=test.test_date,
                ).order_by('-id')
            )
            mark = next(
                (m for m in candidate_marks if _normalize_subject(m.subject) == expected_subject),
                candidate_marks[0] if candidate_marks else None,
            )
            logger.warning(
                'TEST_RESULT_MARK_MATCH_DEBUG student_id=%s test_id=%s subject=%s test_name=%s date=%s candidates=%s selected_mark_id=%s',
                student.id,
                test.id,
                expected_subject,
                ' '.join(str(test.test_name or '').strip().lower().split()),
                str(test.test_date),
                len(candidate_marks),
                mark.id if mark else None,
            )
            if not mark:
                return Response({'error': 'Result not available yet.'}, status=status.HTTP_404_NOT_FOUND)
            total_marks = int(mark.total_marks or 0)
            score = float(mark.marks_obtained or 0)
            accuracy = round((score / total_marks) * 100, 2) if total_marks else 0
            stats = {
                'score': score,
                'total_marks': total_marks,
                'accuracy': accuracy,
                'attempt_rate': 0,
                'correct_count': int(score),
                'incorrect_count': max(0, total_marks - int(score)),
                'unattempted_count': 0,
                'time_taken_seconds': 0,
            }

        return Response({
            'score': stats['score'],
            'total_marks': stats['total_marks'],
            'accuracy': stats['accuracy'],
            'attempt_rate': stats['attempt_rate'],
            'correct': stats['correct_count'],
            'incorrect': stats['incorrect_count'],
            'unattempted': stats['unattempted_count'],
            'time_taken_seconds': stats['time_taken_seconds'],
            'status': 'Completed',
        }, status=status.HTTP_200_OK)


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
        assigned_class = self.request.query_params.get('assigned_class')

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
        
        # Filter by teacher's assigned class (show notifications from students in that class)
        if assigned_class and assigned_class != 'Class N/A':
            queryset = queryset.filter(student__class_name=assigned_class)

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


# ── BULK MARK ENTRY VIEW ──────────────────────────────────────────────────────

class BulkMarkEntryView(APIView):
    """
    POST /api/testmarks/bulk/

    Submit marks for all students in a test at once.
    The test's class_name is used to validate that only students
    belonging to that class can have marks entered.

    Request body:
    {
        "test_id": 5,
        "marks": [
            {"student_id": 1, "marks_obtained": 78},
            {"student_id": 2, "marks_obtained": 65},
            {"student_id": 3, "marks_obtained": 90}
        ]
    }

    Response: list of created TestMark ids.
    """
    def post(self, request):
        return Response(
            {
                'error': (
                    'Manual mark entry is disabled. Marks are automatically calculated '
                    'from student MCQ submissions (1 mark per correct answer).'
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


def _get_or_create_default_admin():
    admin = AdminCredential.objects.order_by('id').first()
    if admin:
        return admin
    return AdminCredential.objects.create(
        username='admin',
        password=make_password('admin123'),
    )


@api_view(['POST'])
def AdminLoginView(request):
    username = request.data.get('username', '').strip()
    password = request.data.get('password', '').strip()

    if not username or not password:
        return Response({'error': 'Username and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    _get_or_create_default_admin()
    admin = AdminCredential.objects.filter(username=username).first()
    if not admin or not check_password(password, admin.password):
        return Response({'error': 'Invalid admin credentials.'}, status=status.HTTP_401_UNAUTHORIZED)

    return Response({'username': admin.username, 'message': 'Admin login successful.'}, status=status.HTTP_200_OK)


@api_view(['POST'])
def AdminChangeCredentialsView(request):
    current_username = request.data.get('current_username', '').strip()
    current_password = request.data.get('current_password', '').strip()
    new_username = request.data.get('new_username', '').strip()
    new_password = request.data.get('new_password', '').strip()

    if not current_username or not current_password or not new_username or not new_password:
        return Response({'error': 'All fields are required.'}, status=status.HTTP_400_BAD_REQUEST)

    _get_or_create_default_admin()
    admin = AdminCredential.objects.filter(username=current_username).first()
    if not admin or not check_password(current_password, admin.password):
        return Response({'error': 'Current credentials are invalid.'}, status=status.HTTP_401_UNAUTHORIZED)

    if len(new_password) < 6:
        return Response({'error': 'New password must be at least 6 characters.'}, status=status.HTTP_400_BAD_REQUEST)

    username_taken = AdminCredential.objects.exclude(pk=admin.pk).filter(username=new_username).exists()
    if username_taken:
        return Response({'error': 'New username is already taken.'}, status=status.HTTP_400_BAD_REQUEST)

    admin.username = new_username
    admin.password = make_password(new_password)
    admin.save(update_fields=['username', 'password', 'updated_at'])
    return Response({'message': 'Admin credentials updated successfully.', 'username': admin.username}, status=status.HTTP_200_OK)


@api_view(['POST'])
def TeacherRegisterView(request):
    teacher_name = request.data.get('teacher_name', '').strip()
    username = request.data.get('username', '').strip().lower()
    password = request.data.get('password', '').strip()
    assigned_class = request.data.get('assigned_class', '').strip()

    if not teacher_name or not username or not password:
        return Response({'error': 'Teacher name, username/email, and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if len(password) < 6:
        return Response({'error': 'Password must be at least 6 characters.'}, status=status.HTTP_400_BAD_REQUEST)

    existing = TeacherCredential.objects.filter(username=username).first()
    if existing and existing.status == 'approved':
        return Response({'error': 'This teacher account is already approved. Please log in.'}, status=status.HTTP_400_BAD_REQUEST)

    if existing and existing.status == 'pending':
        return Response({'error': 'A request with this username/email is already pending approval.'}, status=status.HTTP_400_BAD_REQUEST)

    if existing and existing.status == 'rejected':
        existing.teacher_name = teacher_name
        existing.assigned_class = assigned_class
        existing.password = make_password(password)
        existing.status = 'pending'
        existing.save(update_fields=['teacher_name', 'assigned_class', 'password', 'status', 'updated_at'])
        return Response({'message': 'Teacher account request submitted and is pending approval.', 'status': 'pending'}, status=status.HTTP_201_CREATED)

    TeacherCredential.objects.create(
        teacher_name=teacher_name,
        username=username,
        password=make_password(password),
        assigned_class=assigned_class,
        status='pending',
    )
    return Response({'message': 'Teacher account request submitted and is pending approval.', 'status': 'pending'}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
def TeacherLoginView(request):
    username = request.data.get('username', '').strip().lower()
    password = request.data.get('password', '').strip()

    if not username or not password:
        return Response({'error': 'Username/email and password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    teacher = TeacherCredential.objects.filter(username=username).first()
    if not teacher or not check_password(password, teacher.password):
        return Response({'error': 'Invalid credentials. Please try again.'}, status=status.HTTP_401_UNAUTHORIZED)

    if teacher.status == 'pending':
        return Response({'error': 'Your account request is waiting for Admin approval.'}, status=status.HTTP_403_FORBIDDEN)

    if teacher.status == 'rejected':
        return Response({'error': 'Your teacher access has been revoked. Contact the administrator.'}, status=status.HTTP_403_FORBIDDEN)

    return Response({
        'id': teacher.id,
        'username': teacher.username,
        'teacher_name': teacher.teacher_name,
        'assigned_class': teacher.assigned_class,
        'role': 'teacher',
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
def PendingTeacherListView(request):
    rows = TeacherCredential.objects.filter(status='pending').order_by('-created_at')
    data = [
        {
            'teacher_id': row.id,
            'teacher_name': row.teacher_name,
            'username': row.username,
            'assigned_class': row.assigned_class,
            'status': row.status,
        }
        for row in rows
    ]
    return Response(data, status=status.HTTP_200_OK)


@api_view(['GET'])
def ApprovedTeacherListView(request):
    rows = TeacherCredential.objects.filter(status='approved').order_by('teacher_name')
    data = [
        {
            'teacher_id': row.id,
            'teacher_name': row.teacher_name,
            'username': row.username,
            'assigned_class': row.assigned_class,
            'status': row.status,
        }
        for row in rows
    ]
    return Response(data, status=status.HTTP_200_OK)


@api_view(['POST'])
def ApproveTeacherView(request, pk):
    teacher = get_object_or_404(TeacherCredential, pk=pk)
    teacher.status = 'approved'
    teacher.save(update_fields=['status', 'updated_at'])
    return Response({'message': 'Teacher approved successfully.'}, status=status.HTTP_200_OK)


@api_view(['POST'])
def RejectTeacherView(request, pk):
    teacher = get_object_or_404(TeacherCredential, pk=pk)
    teacher.status = 'rejected'
    teacher.save(update_fields=['status', 'updated_at'])
    return Response({'message': 'Teacher request rejected.'}, status=status.HTTP_200_OK)


@api_view(['DELETE'])
def RevokeTeacherAccessView(request, pk):
    teacher = get_object_or_404(TeacherCredential, pk=pk)
    teacher.status = 'rejected'
    teacher.save(update_fields=['status', 'updated_at'])
    return Response({'message': 'Teacher access removed.'}, status=status.HTTP_200_OK)


@api_view(['POST'])
def TeacherClassChatView(request):
    assigned_class = str(request.data.get('assigned_class', '')).strip()
    message = str(request.data.get('message', '')).strip()
    history = request.data.get('history', [])

    if not assigned_class:
        return Response({'error': 'assigned_class is required.'}, status=status.HTTP_400_BAD_REQUEST)
    if not message:
        return Response({'error': 'message is required.'}, status=status.HTTP_400_BAD_REQUEST)

    students = list(Student.objects.filter(class_name=assigned_class).order_by('name'))
    if not students:
        return Response({'reply': f'No students were found in class {assigned_class}.'}, status=status.HTTP_200_OK)

    student_ids = [s.id for s in students]
    marks = list(TestMark.objects.filter(student_id__in=student_ids).order_by('date_taken', 'id'))

    overall_scores = []
    subject_bucket = {}
    student_bucket = {}

    for mark in marks:
        if not mark.total_marks:
            continue
        pct = round((mark.marks_obtained / mark.total_marks) * 100, 2)
        overall_scores.append(pct)

        subject = _subject_label(mark.subject)
        subject_bucket.setdefault(subject, []).append(pct)

        student_bucket.setdefault(mark.student_id, {'name': mark.student.name, 'scores': []})
        student_bucket[mark.student_id]['scores'].append(pct)

    class_avg = round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else 0
    subject_lines = []
    for subject, scores in sorted(subject_bucket.items(), key=lambda item: item[0]):
        avg = round(sum(scores) / len(scores), 2) if scores else 0
        subject_lines.append(f"- {subject}: {avg}% average")

    student_averages = []
    for info in student_bucket.values():
        scores = info['scores']
        avg = round(sum(scores) / len(scores), 2) if scores else 0
        student_averages.append((info['name'], avg))

    student_averages.sort(key=lambda row: row[1])
    lowest = student_averages[:3]
    improving_hint = sorted(student_averages, key=lambda row: row[1], reverse=True)[:3]

    class_context = (
        "You are a class performance assistant for a teacher.\n"
        "Use only the class data provided below.\n"
        "Avoid generic advice and keep answers concise and actionable.\n\n"
        f"Assigned class: {assigned_class}\n"
        f"Total students: {len(students)}\n"
        f"Marks records available: {len(marks)}\n"
        f"Class average score: {class_avg}%\n\n"
        "Subject averages:\n"
        + ("\n".join(subject_lines) if subject_lines else "- No subject marks available yet")
        + "\n\n"
        + "Students needing attention (lowest averages):\n"
        + (
            "\n".join([f"- {name}: {avg}%" for name, avg in lowest])
            if lowest else "- No student-level averages available yet"
        )
        + "\n\n"
        + "Top current performers:\n"
        + (
            "\n".join([f"- {name}: {avg}%" for name, avg in improving_hint])
            if improving_hint else "- No student-level averages available yet"
        )
    )

    conversation = []
    if isinstance(history, list):
        for turn in history:
            if not isinstance(turn, dict):
                continue
            role = turn.get('role')
            parts = turn.get('parts')
            if role not in {'user', 'model'}:
                continue
            if not isinstance(parts, list) or not parts:
                continue
            conversation.append({'role': role, 'parts': [str(parts[0])[:2000]]})

    conversation.append({'role': 'user', 'parts': [message]})

    reply = chat_with_student_context(class_context, conversation)
    if isinstance(reply, str) and reply.startswith('Error:'):
        if not marks:
            reply = (
                f"Class {assigned_class} has no marks yet, so I can only help with planning and next steps."
            )
        else:
            weakest_subject = min(
                subject_bucket.items(),
                key=lambda item: (sum(item[1]) / len(item[1])) if item[1] else 0,
            )[0] if subject_bucket else 'General'
            reply = (
                f"Current class average is {class_avg}%. The lowest-performing subject trend is {weakest_subject}. "
                "Prioritize revision for bottom-performing students first."
            )
    return Response({'reply': reply}, status=status.HTTP_200_OK)
