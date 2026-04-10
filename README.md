# Academic Performance Tracking System - Backend

This backend powers an academic performance platform where Admins, Teachers, and Students collaborate around tests, marks, and AI-supported learning feedback.

## Project Purpose

The system helps schools and colleges:
- Manage students and class records
- Organize upcoming tests and question banks
- Collect student submissions and compute results
- Track topic-wise strengths and weaknesses
- Alert students and teachers when performance drops
- Provide AI-generated guidance and tutoring responses

## Core Functional Areas

### 1. Identity and Access
- Admin authentication and admin credential update
- Teacher registration with approval/rejection workflow
- Teacher login with class-level access
- Student login and password change

### 2. Academic Data Management
- Student CRUD
- Subject catalog management
- Test mark management (single and bulk)
- Upcoming test creation with scheduling metadata

### 3. Test Lifecycle
- Publish tests with question banks
- Student test details and questions endpoints
- Submission, review, and result generation
- Attempt tracking and analytics persistence

### 4. AI Intelligence Layer
- AI analysis result storage per student-test pair
- Conceptual mistake and behavior pattern extraction
- Topic-wise recommendations
- AI tutor chat endpoint using student context
- Rate limiting and cooldown controls for LLM calls

### 5. Notification System
- Test schedule notifications
- Student warnings for sudden/gradual decline
- Teacher alerts on repeated low performance
- Read/unread tracking and mark-all-read support

## Backend Architecture

- Framework: Django + Django REST Framework
- App module: tracker
- Main project module: backend
- Data stores:
	- PostgreSQL in production
	- SQLite fallback in local development when DATABASE_URL is absent and DEBUG is true

Key domain models include:
- Student
- TeacherCredential
- UpcomingTest
- TestQuestion
- TestResult
- TestAttempt
- AIAnalysisResult
- Notification

## API Surface

Base route: /api/

Major route groups:
- /students/
- /subjects/
- /testmarks/
- /upcoming-tests/
- /notifications/
- /admin-login/
- /teacher-register/
- /teacher-login/

## Environment Configuration

Required production environment variables:
- DJANGO_SECRET_KEY
- DJANGO_DEBUG=false
- DJANGO_ALLOWED_HOSTS
- DATABASE_URL
- DJANGO_CORS_ALLOWED_ORIGINS
- DJANGO_CSRF_TRUSTED_ORIGINS

AI-related environment variables:
- GOOGLE_API_KEY
- APT_AI_LLM_ENABLED
- APT_AI_BACKGROUND_ANALYSIS_ENABLED
- APT_AI_LLM_MODEL
- APT_AI_LLM_MAX_CALLS_PER_MINUTE
- APT_AI_LLM_COOLDOWN_SECONDS
- APT_AI_LLM_MAX_RETRIES
- APT_AI_LLM_BACKOFF_SECONDS

## Run and Deploy

Local run:
1. pip install -r requirements.txt
2. python manage.py migrate
3. python manage.py runserver

Render deployment:
- Build command: pip install -r requirements.txt
- Start command: gunicorn backend.wsgi:application --bind 0.0.0.0:$PORT

After deployment:
1. python manage.py migrate
2. optionally python manage.py collectstatic --noinput

## Project Note

Teacher portal users are stored in the TeacherCredential table and are intentionally separate from Django auth users.
