from rest_framework import serializers
from django.contrib.auth.hashers import make_password
from .models import Student, TestMark, Attendance , Subject, UpcomingTest, Notification


class TestMarkSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestMark
        fields = ['id', 'student', 'subject', 'test_name', 'marks_obtained', 'total_marks', 'date_taken']


class AttendanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attendance
        fields = ['id', 'student', 'date', 'status']


class StudentSerializer(serializers.ModelSerializer):
    test_marks = TestMarkSerializer(source='testmark_set', many=True, read_only=True)
    student_password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    def create(self, validated_data):
        raw_password = validated_data.pop('student_password', '').strip() or 'student-123'
        validated_data['student_password'] = make_password(raw_password)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        raw_password = validated_data.pop('student_password', None)
        if raw_password is not None and str(raw_password).strip():
            validated_data['student_password'] = make_password(str(raw_password).strip())
        return super().update(instance, validated_data)

    class Meta:
        model = Student
        fields = [
            'id',
            'roll_number',
            'student_number',
            'student_email',
            'student_password',
            'name',
            'class_name',
            'dob',
            'gender',
            'nationality',
            'blood_group',
            'parent_name',
            'parent_number',
            'parent_email',
            'address',
            'emergency_contact',
            'test_marks',
        ]

class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ['id', 'name']


class UpcomingTestSerializer(serializers.ModelSerializer):
    subject = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        subject = (attrs.get('subject') or '').strip()
        topic = (attrs.get('topic') or '').strip()
        resolved = subject or topic
        if not resolved:
            raise serializers.ValidationError({'subject': 'Subject is required.'})

        attrs['subject'] = resolved
        attrs['topic'] = topic or resolved
        return attrs

    class Meta:
        model = UpcomingTest
        fields = ['id', 'test_name', 'subject', 'topic', 'test_date', 'total_marks', 'class_name', 'created_at']


class NotificationSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.name', read_only=True)
    class_name = serializers.CharField(source='student.class_name', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id',
            'student',
            'student_name',
            'class_name',
            'recipient_role',
            'type',
            'subject',
            'message',
            'timestamp',
            'read_status',
            'details',
        ]

    timestamp = serializers.DateTimeField(source='created_at', read_only=True)