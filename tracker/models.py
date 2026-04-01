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


class TestQuestion(models.Model):
    QUESTION_TYPE_CHOICES = [
        ('MCQ', 'Multiple Choice'),
        ('SHORT_ANSWER', 'Short Answer'),
        ('TRUE_FALSE', 'True/False'),
    ]
    DIFFICULTY_CHOICES = [
        ('Easy', 'Easy'),
        ('Medium', 'Medium'),
        ('Hard', 'Hard'),
    ]

    test = models.ForeignKey('UpcomingTest', on_delete=models.CASCADE, related_name='questions')
    question_text = models.TextField()
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES, default='MCQ')
    options = models.JSONField(blank=True, default=dict)
    correct_answer = models.CharField(max_length=255)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, default='Medium')
    topic = models.CharField(max_length=255, blank=True)
    marks = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=50, default='AI_GEMINI')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test', 'created_at']

    def __str__(self):
        return f"{self.test.test_name} - {self.question_text[:50]}"


class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class UpcomingTest(models.Model):
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('active', 'Active'),
        ('finished', 'Finished'),
    ]

    test_name = models.CharField(max_length=120)
    subject = models.CharField(max_length=120, default='General')
    topic = models.CharField(max_length=255, blank=True, default='')
    test_date = models.DateField()
    start_time = models.DateTimeField(blank=True, null=True)
    end_time = models.DateTimeField(blank=True, null=True)
    num_questions = models.PositiveIntegerField(default=50)
    study_material = models.FileField(upload_to='study_materials/', blank=True, null=True)
    questions_generated = models.BooleanField(default=False)
    question_bank = models.JSONField(blank=True, default=list)
    total_marks = models.PositiveIntegerField()
    class_name = models.CharField(max_length=50)
    teacher_id = models.IntegerField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['test_date', '-created_at']

    def __str__(self):
        return f"{self.test_name} - {self.subject} ({self.class_name})"


class StudentTestResponse(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='test_responses')
    test = models.ForeignKey(UpcomingTest, on_delete=models.CASCADE, related_name='student_responses')
    question = models.ForeignKey(TestQuestion, on_delete=models.CASCADE, related_name='student_answers')
    student_answer = models.TextField()
    is_correct = models.BooleanField(default=False)
    marks_obtained = models.FloatField(default=0)
    response_time = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['submitted_at']
        unique_together = ('student', 'question', 'test')


class TestResult(models.Model):
    STATUS_CHOICES = [
        ('Completed', 'Completed'),
        ('Expired', 'Expired'),
        ('InProgress', 'In Progress'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='test_results')
    test = models.ForeignKey(UpcomingTest, on_delete=models.CASCADE, related_name='results')
    total_score = models.FloatField()
    total_marks = models.FloatField()
    percentage = models.FloatField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='InProgress')
    topic_wise_analysis = models.JSONField(default=dict)
    strengths = models.JSONField(default=list)
    weaknesses = models.JSONField(default=list)
    recommendations = models.TextField(blank=True)
    predicted_performance = models.JSONField(default=dict)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-completed_at']
        unique_together = ('student', 'test')


class TestAttempt(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='test_attempts')
    test = models.ForeignKey(UpcomingTest, on_delete=models.CASCADE, related_name='test_attempts')
    answers_payload = models.JSONField(blank=True, default=list)
    conceptual_patterns = models.JSONField(blank=True, default=list)
    behavior_patterns = models.JSONField(blank=True, default=list)
    score = models.FloatField(default=0)
    total_marks = models.FloatField(default=0)
    correct_count = models.PositiveIntegerField(default=0)
    incorrect_count = models.PositiveIntegerField(default=0)
    unattempted_count = models.PositiveIntegerField(default=0)
    attempted_count = models.PositiveIntegerField(default=0)
    accuracy = models.FloatField(default=0)
    attempt_rate = models.FloatField(default=0)
    time_taken_seconds = models.PositiveIntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        unique_together = ('student', 'test')


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


class AdminCredential(models.Model):
    username = models.CharField(max_length=100, unique=True)
    password = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Admin: {self.username}"


class TeacherCredential(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]

    teacher_name = models.CharField(max_length=120)
    username = models.CharField(max_length=120, unique=True)
    password = models.CharField(max_length=128)
    assigned_class = models.CharField(max_length=120, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['status', '-created_at']

    def __str__(self):
        return f"{self.teacher_name} ({self.username}) - {self.status}"
