from django.db import models


class Student(models.Model):
    name = models.CharField(max_length=100)
    roll_number = models.CharField(max_length=20, unique=True)
    student_number = models.CharField(max_length=20, unique=True, blank=True, null=True)
    student_email = models.EmailField(unique=True, blank=True, null=True)
    # Stores hashed password for student portal login.
    student_password = models.CharField(max_length=128, default='student-123')
    class_name = models.CharField(max_length=50)
    parent_number = models.CharField(max_length=15, blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    nationality = models.CharField(max_length=50, blank=True, null=True)
    blood_group = models.CharField(max_length=10, blank=True, null=True)
    parent_name = models.CharField(max_length=100, blank=True, null=True)
    parent_email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    emergency_contact = models.CharField(max_length=15, blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.roll_number})"


class TestMark(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    subject = models.CharField(max_length=50, default="General")
    test_name = models.CharField(max_length=100)
    marks_obtained = models.FloatField()
    total_marks = models.FloatField()
    date_taken = models.DateField()

    def __str__(self):
        return f"{self.student.name} - {self.test_name}"


class Attendance(models.Model):
    STATUS_CHOICES = [
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('not_marked', 'Not Marked'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_marked')

    class Meta:
        # One record per student per date — no duplicates
        unique_together = ('student', 'date')

    def __str__(self):
        return f"{self.student.name} - {self.date} - {self.status}"

class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class UpcomingTest(models.Model):
    test_name = models.CharField(max_length=120)
    subject = models.CharField(max_length=120, default='General')
    topic = models.CharField(max_length=255, blank=True, default='')
    test_date = models.DateField()
    total_marks = models.PositiveIntegerField()
    class_name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_date', '-created_at']

    def __str__(self):
        return f"{self.test_name} - {self.subject} ({self.class_name})"


class Notification(models.Model):
    TYPE_CHOICES = [
        ('test', 'Test Notification'),
        ('ai_warning', 'AI Warning'),
        ('teacher_alert', 'Teacher Alert'),
    ]

    RECIPIENT_CHOICES = [
        ('student', 'Student'),
        ('teacher', 'Teacher'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notifications')
    recipient_role = models.CharField(max_length=20, choices=RECIPIENT_CHOICES, default='student')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    subject = models.CharField(max_length=120, blank=True)
    message = models.TextField()
    event_key = models.CharField(max_length=180, unique=True)
    read_status = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(blank=True, default=dict)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.recipient_role}:{self.type}:{self.student.name}"