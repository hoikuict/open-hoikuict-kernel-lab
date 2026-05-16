# データ辞書

この表は `demo_data/full/` 以下のCSVを対象にしています。`import_compatible/` は既存インポート画面で新規登録する想定のため、ID列を空欄にしています。

| CSV | 件数 | 主な列 |
|---|---:|---|
| `classrooms` | 6 | id, name, display_order |
| `families` | 84 | id, family_name, home_address, home_phone, shared_profile, created_at, updated_at |
| `children` | 100 | id, last_name, first_name, last_name_kana, first_name_kana, birth_date, enrollment_date, withdrawal_date, status, classroom_id, family_id, home_address, home_phone, older_sibling_id, extra_data, created_at, updated_at |
| `guardians` | 210 | id, child_id, last_name, first_name, last_name_kana, first_name_kana, relationship, phone, workplace, workplace_address, workplace_phone, order |
| `parent_accounts` | 156 | id, display_name, email, phone, home_address, workplace, workplace_address, workplace_phone, family_id, status, password_hash, invited_at, last_login_at, created_at, updated_at |
| `parent_child_links` | 186 | id, parent_account_id, child_id, relationship_label, is_primary_contact, created_at |
| `child_health_profiles` | 100 | id, child_id, blood_type, primary_doctor_name, primary_doctor_phone, primary_doctor_address, hospital_name, hospital_phone, requires_medical_care, medical_care_details, epipen_required, epipen_storage_location, medical_history, disability_info, current_medications, sids_risk_flag, sids_notes, breastfed, formula_type, food_texture_level, religious_dietary, other_dietary_restrictions, developmental_notes, psychological_notes, family_health_notes, other_notes, extra_data, created_by, updated_by, created_at, updated_at |
| `child_allergies` | 9 | id, child_id, allergen_category, allergen_name, severity, symptoms, diagnosis_confirmed, diagnosis_date, treating_doctor, removal_required, substitute_food, action_plan, source_document, source_document_date, valid_until, is_active, notes, created_by, updated_by, created_at, updated_at |
| `health_check_records` | 200 | id, child_id, check_type, checked_at, height_cm, weight_kg, head_circumference_cm, chest_circumference_cm, temperature, heart_rate, respiratory_rate, vision_right, vision_left, hearing_result, dental_result, overall_result, doctor_name, requires_followup, followup_notes, general_condition, observer_name, created_by, updated_by, created_at, updated_at |
| `users` | 19 | id, email, display_name, timezone, locale, default_calendar_id, staff_role, staff_sort_order, is_calendar_admin, is_active, created_at, updated_at |
| `calendars` | 21 | id, owner_user_id, name, calendar_type, color, description, is_primary, is_archived, created_at, updated_at |
| `calendar_members` | 57 | id, calendar_id, user_id, role, created_at, updated_at |
| `calendar_user_preferences` | 57 | id, calendar_id, user_id, is_visible, display_order, created_at, updated_at |
| `events` | 13 | id, calendar_id, created_by_user_id, kind, title, description, location, start_at, end_at, timezone, is_all_day, visibility, status, recurrence_rule_id, split_from_event_id, split_from_original_start_at, is_deleted, created_at, updated_at |
| `daily_contact_entries` | 2090 | id, child_id, parent_account_id, target_date, temperature, sleep_notes, breakfast_status, bowel_movement_status, mood, cough, runny_nose, medication, condition_note, contact_note, contact_type, absence_temperature, absence_symptoms, absence_diagnosis, absence_note, status, extra_data, submitted_at, created_at, updated_at |
| `attendance_records` | 2100 | id, child_id, attendance_date, check_in_at, check_out_at, planned_pickup_time, pickup_person, note, created_at, updated_at |
| `attendance_verifications` | 2100 | id, child_id, target_date, status, updated_by_name, created_at, updated_at |
| `attendance_verification_histories` | 2100 | id, child_id, target_date, status, updated_by_name, created_at |
| `attendance_alarm_states` | 13 | id, child_id, target_date, is_active, reasons, evaluated_at, created_at, updated_at |
| `attendance_alarm_histories` | 13 | id, child_id, target_date, is_active, reasons, evaluated_at, created_at |
| `notices` | 8 | id, title, body, priority, status, publish_start_at, publish_end_at, created_by, created_at, updated_at |
| `notice_targets` | 8 | id, notice_id, target_type, target_value, created_at |
| `notice_reads` | 676 | id, notice_id, parent_account_id, read_at |
| `messages` | 24 | id, room_id, parent_message_id, author_name, body, created_at, updated_at, deleted_at, deleted_by |
| `surveys` | 1 | id, title, description, status, audience_type, answer_unit, opens_at, closes_at, created_by, updated_by, created_at, updated_at |
| `survey_targets` | 1 | id, survey_id, target_type, target_value, created_at |
| `survey_questions` | 3 | id, survey_id, order, question_type, label, description, is_required, created_at, updated_at |
| `survey_question_options` | 4 | id, question_id, order, option_key, label, created_at, updated_at |
| `survey_answers` | 68 | id, survey_id, family_id, child_id, staff_user_id, created_by_parent_account_id, created_by_staff_user_id, submitted_by_parent_account_id, submitted_by_staff_user_id, submitted_at, created_at, updated_at |
| `survey_responses` | 144 | id, answer_id, question_id, value_text, value_option_ids, value_scale, value_bool, value_date, created_at, updated_at |
| `profile_change_notifications` | 10 | id, parent_account_id, change_summary, change_details, is_read, created_at, read_at |
| `child_profile_change_requests` | 10 | id, child_id, parent_account_id, status, change_summary, request_data, change_details, submitted_at, reviewed_at, reviewed_by, review_note, updated_at |

## 補足

- 登降園記録は、園児100人 × 対象日21日分で 2,100件です。
- 日次連絡は、連絡未提出のケースを含めているため 2,090件です。
- `created_by`、`updated_by`、`reviewed_by` の一部には、ユーザーIDではなく画面表示用の担当者名を入れています。
