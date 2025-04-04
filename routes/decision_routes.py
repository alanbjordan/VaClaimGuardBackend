# routes/decision_routes.py

from flask import Blueprint, request, jsonify, g
from models.sql_models import UserDecisionSaves, Users
from database.session import ScopedSession
from config import Config
import jwt  # We likely don't need this import anymore if token-based logic is removed
from datetime import datetime
from helpers.llm_helpers import structured_summarize_bva_decision_llm
from helpers.decision_helper import summarize_decision

decision_bp = Blueprint('decision_bp', __name__)

def log_with_timing(prev_time, message):
    current_time = datetime.utcnow()
    if prev_time is None:
        elapsed = 0.0
    else:
        elapsed = (current_time - prev_time).total_seconds()
    print(f"[{current_time.isoformat()}] {message} (Elapsed: {elapsed:.4f}s)")
    return current_time

@decision_bp.before_request
def create_session():
    t = log_with_timing(None, "[BEFORE_REQUEST] Creating session...")
    g.session = ScopedSession()
    t = log_with_timing(t, "[BEFORE_REQUEST] Session created and attached to g.")

@decision_bp.teardown_request
def remove_session(exception=None):
    t = log_with_timing(None, "[TEARDOWN_REQUEST] Starting teardown...")
    session = g.pop('session', None)
    if session:
        if exception:
            t = log_with_timing(t, "[TEARDOWN_REQUEST] Exception detected. Rolling back session.")
            session.rollback()
        else:
            t = log_with_timing(t, "[TEARDOWN_REQUEST] No exception. Committing session.")
            session.commit()
        ScopedSession.remove()
        t = log_with_timing(t, "[TEARDOWN_REQUEST] Session removed.")
    else:
        t = log_with_timing(t, "[TEARDOWN_REQUEST] No session found.")

### CHANGED: Remove all token-based logic and define a new function for user_uuid header.

def get_user_uuid_from_request(request):
    t = log_with_timing(None, "[get_user_uuid_from_request] Extracting user_uuid from headers...")
    user_uuid = request.headers.get("user-uuid")
    if not user_uuid:
        t = log_with_timing(t, "[get_user_uuid_from_request][ERROR] user-uuid header missing.")
        return None, "user-uuid header is required."
    t = log_with_timing(t, f"[get_user_uuid_from_request] user_uuid found: {user_uuid}")
    return user_uuid, None

@decision_bp.route('/user_decision_save', methods=['GET', 'POST'])
def user_decision_save():
    t = log_with_timing(None, f"[user_decision_save] Route called with method {request.method}")

    user_uuid, error = get_user_uuid_from_request(request)
    if error:
        t = log_with_timing(t, f"[user_decision_save][WARNING] Missing user_uuid: {error}")
        return jsonify({"error": error}), 400

    session = g.session
    t = log_with_timing(t, f"[user_decision_save] Fetching user from DB for user_uuid: {user_uuid}")
    user = session.query(Users).filter_by(user_uuid=user_uuid).first()

    if not user:
        t = log_with_timing(t, f"[user_decision_save][WARNING] User not found for user_uuid: {user_uuid}")
        return jsonify({"error": "Invalid user UUID"}), 404

    if request.method == 'GET':
        t = log_with_timing(t, "[user_decision_save][GET] Processing GET request...")
        decision_citation = request.args.get('decision_citation')
        if not decision_citation:
            t = log_with_timing(t, "[user_decision_save][GET][WARNING] Missing decision_citation parameter.")
            return jsonify({"error": "decision_citation is required"}), 400

        t = log_with_timing(t, f"[user_decision_save][GET] Querying for user_id={user.user_id}, citation={decision_citation}")
        uds = session.query(UserDecisionSaves).filter_by(
            user_id=user.user_id,
            decision_citation=decision_citation
        ).first()
        if uds:
            t = log_with_timing(t, f"[user_decision_save][GET] Found existing record (id={uds.id}). Returning record.")
            notes_with_summary = uds.notes or {}
            if 'summary' not in notes_with_summary:
                notes_with_summary['summary'] = ""

            return jsonify({
                "id": uds.id,
                "decision_citation": uds.decision_citation,
                "decision_url": uds.decision_url,  # Include decision_url
                "notes": notes_with_summary,
                "created_at": uds.created_at.isoformat(),
                "updated_at": uds.updated_at.isoformat()
            }), 200
        else:
            t = log_with_timing(t, "[user_decision_save][GET] No record found. Returning empty notes with summary.")
            return jsonify({"notes": {"comments": [], "highlights": [], "summary": ""}}), 200

    elif request.method == 'POST':
        t = log_with_timing(t, "[user_decision_save][POST] Processing POST request...")
        data = request.get_json()
        t = log_with_timing(t, f"[user_decision_save][POST] JSON data received: {data}")
        decision_citation = data.get('decision_citation')
        decision_url = data.get('decision_url')  # Extract decision_url from the request
        if not decision_citation:
            t = log_with_timing(t, "[user_decision_save][POST][WARNING] Missing decision_citation in request body.")
            return jsonify({"error": "decision_citation is required"}), 400

        notes = data.get('notes', {})
        if 'summary' not in notes:
            notes['summary'] = ""

        t = log_with_timing(t, f"[user_decision_save][POST] Checking if record exists for user_id={user.user_id}, citation={decision_citation}")
        uds = session.query(UserDecisionSaves).filter_by(
            user_id=user.user_id,
            decision_citation=decision_citation
        ).first()

        if uds:
            t = log_with_timing(t, f"[user_decision_save][POST] Record found (id={uds.id}). Updating notes and URL.")
            uds.notes = notes
            uds.decision_url = decision_url  # Update the decision_url
            uds.updated_at = datetime.utcnow()
        else:
            t = log_with_timing(t, "[user_decision_save][POST] No existing record. Creating new entry.")
            uds = UserDecisionSaves(
                user_id=user.user_id,
                decision_citation=decision_citation,
                decision_url=decision_url,  # Save the decision_url
                notes=notes
            )
            session.add(uds)

        session.commit()
        t = log_with_timing(t, "[user_decision_save][POST] Notes, URL, and summary saved/updated successfully.")
        return jsonify({"message": "Notes, URL, and summary saved successfully"}), 200

@decision_bp.route('/structured_summarize_bva_decision', methods=['POST'])
def structured_summarize_bva_decision():
    t = log_with_timing(None, f"[structured_summarize_bva_decision] Route called with method {request.method}")

    ### CHANGED: Again, we use the new helper
    user_uuid, error = get_user_uuid_from_request(request)
    if error:
        t = log_with_timing(t, f"[structured_summarize_bva_decision][WARNING] Missing user_uuid: {error}")
        return jsonify({"error": error}), 400

    session = g.session
    t = log_with_timing(t, f"[structured_summarize_bva_decision] Fetching user from DB for user_uuid: {user_uuid}")
    user = session.query(Users).filter_by(user_uuid=user_uuid).first()
    if not user:
        t = log_with_timing(t, f"[structured_summarize_bva_decision][WARNING] User not found for user_uuid: {user_uuid}")
        return jsonify({"error": "Invalid user UUID"}), 404

    data = request.get_json()
    t = log_with_timing(t, f"[structured_summarize_bva_decision] Received JSON data: {data}")
    decision_citation = data.get('decision_citation')
    full_text = data.get('fullText')

    if not decision_citation or not full_text:
        t = log_with_timing(t, "[structured_summarize_bva_decision][WARNING] Missing required fields: decision_citation or fullText.")
        return jsonify({"error": "decision_citation and fullText are required"}), 400

    try:
        t = log_with_timing(t, f"[structured_summarize_bva_decision] Calling structured_summarize_bva_decision_llm(...)")
        structured_data = structured_summarize_bva_decision_llm(user.user_id, decision_citation, full_text)
        t = log_with_timing(t, f"[structured_summarize_bva_decision] Received structured_data.")
    except Exception as e:
        t = log_with_timing(t, f"[structured_summarize_bva_decision][ERROR] Error extracting structured summary: {e}")
        return jsonify({"error": "Could not generate structured summary."}), 500

    t = log_with_timing(t, "[structured_summarize_bva_decision] Returning structured_data.")
    return jsonify(structured_data), 200

@decision_bp.route('/user_decisions', methods=['GET'])
def get_all_user_decisions():
    t = log_with_timing(None, f"[get_all_user_decisions] Route called with method {request.method}")

    # Extract user UUID from request headers
    user_uuid, error = get_user_uuid_from_request(request)
    if error:
        t = log_with_timing(t, f"[get_all_user_decisions][WARNING] Missing user_uuid: {error}")
        return jsonify({"error": error}), 400

    session = g.session
    t = log_with_timing(t, f"[get_all_user_decisions] Fetching user from DB for user_uuid: {user_uuid}")
    user = session.query(Users).filter_by(user_uuid=user_uuid).first()

    if not user:
        t = log_with_timing(t, f"[get_all_user_decisions][WARNING] User not found for user_uuid: {user_uuid}")
        return jsonify({"error": "Invalid user UUID"}), 404

    t = log_with_timing(t, f"[get_all_user_decisions] Querying saved decisions for user_id={user.user_id}")
    saved_decisions = session.query(UserDecisionSaves).filter_by(user_id=user.user_id).all()

    # Format the response data
    decisions_data = [
        {
            "id": decision.id,
            "decision_citation": decision.decision_citation,
            "decision_url": decision.decision_url,  # Add this line
            "notes": decision.notes,
            "created_at": decision.created_at.isoformat(),
            "updated_at": decision.updated_at.isoformat()
        }
        for decision in saved_decisions
    ]

    t = log_with_timing(t, "[get_all_user_decisions] Returning saved decisions.")
    return jsonify({"saved_decisions": decisions_data}), 200

###############################################################################
# summarize_decision
###############################################################################
@decision_bp.route('/summarize_decision', methods=['POST','OPTIONS'])
def summarize_decision_route():
    """
    Route to summarize a BVA decision text.
    Expects JSON payload with 'decision_text'.
    Returns a structured summary as per BvaDecisionStructuredSummary model.
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        print("[DEBUG] /summarize_decision: Handling OPTIONS preflight.")
        response = jsonify({"message": "CORS preflight successful"})
        response.headers.update({
            "Access-Control-Allow-Origin": Config.CORS_ORIGINS,
            "Access-Control-Allow-Headers": "Content-Type, Authorization, user-uuid",
            "Access-Control-Allow-Methods": "GET, PUT, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Credentials": "true"
        })
        return response, 200

    print("[DEBUG] /summarize_decision: Entered POST method.")
    try:
        # 1) Get user_uuid from request
        user_uuid, error = get_user_uuid_from_request(request)
        print(f"[DEBUG] user_uuid from request: {user_uuid}, error: {error}")
        if error:
            print("[DEBUG] Missing or invalid user_uuid.")
            return jsonify({"error": error}), 400

        # 2) Look up user
        print("[DEBUG] Querying database for user...")
        user = g.session.query(Users).filter_by(user_uuid=user_uuid).first()
        if not user:
            print("[DEBUG] User not found for given user_uuid.")
            return jsonify({"error": "Invalid user UUID"}), 404

        print(f"[DEBUG] Found user_id={user.user_id}, credits_remaining={user.credits_remaining}")

        # 3) Check that the user has at least 10,000 credits
        if user.credits_remaining < 10000:
            print("[DEBUG] User does NOT have enough credits.")
            return jsonify({"error": f"You need at least 10,000 credits to summarize this decision. You have {user.credits_remaining} credits remaining, please add more credits to your account."}), 403

        print("[DEBUG] User has enough credits. Proceeding...")

        # 4) Parse JSON for the decision_text
        data = request.json
        if not data:
            print("[DEBUG] No JSON body in request.")
            return jsonify({"error": "Missing JSON body"}), 400

        decision_text = data.get('decision_text')
        if not decision_text:
            print("[DEBUG] No 'decision_text' field in JSON.")
            return jsonify({"error": "decision_text is required"}), 400

        print("[DEBUG] Received /summarize_decision request with valid user and data.")

        # 5) Call the summarize_decision helper with user_id
        print("[DEBUG] Calling summarize_decision helper now...")
        structured_summary = summarize_decision(decision_text, user.user_id)

        if not structured_summary:
            print("[DEBUG] summarize_decision returned None. Possibly ValidationError or other issue.")
            return jsonify({"error": "Failed to generate structured summary."}), 500

        # 6) Return the structured summary in JSON form
        print("[DEBUG] summarize_decision succeeded. Returning structured_summary.")
        return jsonify(structured_summary.dict()), 200

    except Exception as e:
        print("Error in summarizing decision:", e)
        import traceback
        traceback.print_exc()  # Print full traceback for debugging
        return jsonify({"error": "Failed to process request."}), 500