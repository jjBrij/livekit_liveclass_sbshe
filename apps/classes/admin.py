from django.contrib import admin
from .models import LiveClass
from .models import ClassParticipant
from .models import Attendance
from .models import ClassMessage
from .models import ClassRecording


@admin.register(LiveClass)
class LiveClassAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "teacher_id", "status", "scheduled_at", "room_name")
    list_filter = ("status",)
    search_fields = ("title", "room_name", "teacher_name")
    readonly_fields = ("created_at", "updated_at", "room_name")
    ordering = ("-scheduled_at",)

@admin.register(ClassParticipant)
class ClassParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "live_class", "user_id", "name", "role", "status", "joined_at", "left_at")
    list_filter = ("role", "status")
    search_fields = ("name", "user_id")
    readonly_fields = ("joined_at",)
    ordering = ("-joined_at",)

@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = (
        "id", "live_class", "user_id", "name", "role",
        "total_duration_seconds", "first_joined_at", "last_left_at",
    )
    list_filter = ("role",)
    search_fields = ("name", "user_id")
    readonly_fields = ("sessions",)
    ordering = ("-first_joined_at",)

@admin.register(ClassMessage)
class ClassMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "live_class", "user_id", "name", "role", "visibility", "created_at", "deleted_at")
    list_filter = ("role", "visibility", "message_type")
    search_fields = ("name", "text")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)    


@admin.register(ClassRecording)
class ClassRecordingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "live_class", "status", "egress_id",
        "started_at", "stopped_at", "duration_seconds", "file_size_bytes",
    )
    list_filter = ("status",)
    search_fields = ("egress_id", "storage_key")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-started_at",)    