# Fix Plan: SWAR-1327 - Session Creation Error Handling

## Root Causes

### 1. Missing session creation template context on IntegrityError

When a duplicate session name is detected during creation, the handler properly rolls back and displays an error using `RedirectResponse`, but fails to pass the form data back to the template for re-rendering. This causes the "persist" field (and other form fields) to be lost, leading to inconsistent states.

### 2. Incomplete IntegrityError handling in session update

Similar to create operation, when updating a session with a duplicate name, the IntegrityError handling doesn't properly restore the original form values and context, leading to potential data loss and a poor user experience.

## Approach

Fix both issues by ensuring that on IntegrityError conditions in session creation and updates:
1. Form data is properly preserved in error state
2. Template context is correctly passed with all necessary form values 
3. Users can easily correct and re-submit forms without re-entering everything

## Files to Modify

### `swarmer/routers/sessions.py`

Two primary fixes needed:

1. **session_create function (lines ~368-395)**:
   - Add missing template context data for form field restoration
   - Include all form values in error response context, not just name and instruction_prompt

2. **session_edit function (lines ~568-572)**:
   - Add proper handling to preserve the session update form state on IntegrityError
   - Ensure original form values are restored for re-submission

### Implementation Plan

1. Add comprehensive form context restoration in `session_create` error handler
2. Add comprehensive form context restoration in `session_edit` error handler  
3. Ensure all relevant form fields (name, instruction_prompt, persist, mode, etc.) are preserved for error re-rendering