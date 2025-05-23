import sys
import os
import requests
import json
import urllib.parse
# Get absolute path to project root (1 level above SearchService)
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Add it to Python's module search path
if project_root not in sys.path:
    sys.path.insert(0, project_root)
import flask
from ViFinanceCrawLib.article_database.ScrapeAndTagArticles import ScrapeAndTagArticles
from ViFinanceCrawLib.QuantAna.QuantAna_albert import QuantAnaInsAlbert
from ViFinanceCrawLib.article_database.ArticleQueryDatabase import AQD
from flask import request, jsonify, request
from urllib.parse import unquote, unquote_plus
import hashlib
from flask_cors import CORS
from LoggingService.app import log_event


UP_VOTE=1
DOWN_VOTE=-1
NEUTRAL_VOTE=0
BACKEND_SERVICE_NAME = "SearchService"

app = flask.Flask(__name__)
# CORS(app, supports_credentials=True, origins=["http://localhost:6999"])

CORS(app, supports_credentials=True, origins=["*"])
quant_analyser = QuantAnaInsAlbert()
scrapped_url = []
processor = ScrapeAndTagArticles()
aqd_object = AQD()
print("Search Started")


@app.route("/api/get_cached_result", methods=['POST'])
@log_event(service_name=BACKEND_SERVICE_NAME, event_base="GetCachedResult")
def get_articles():
    data = request.get_json()
    
    user_query = data.get("query", "").strip() if data else ""
    if not user_query or quant_analyser.obsence_check(query=user_query):
        return jsonify({"error": "Yêu cầu tìm kiếm của bạn vi phạm về điều khoản tìm kiếm nội dung an toàn của chúng tôi"}), 400

    
    try:
        scraped_data = processor.search_and_scrape(user_query)
        print("Scrape sucesss")
        if not scraped_data:
            return jsonify({"error": "No results found"}), 404
        
        session_id = request.cookies.get('SESSION_ID')
        user_id = aqd_object.get_userID_from_session(SESSION_ID=session_id)
        
        if user_id is not None: # Save the user search-history
            aqd_object.move_query_to_history(user_id, user_query.encode())

        for article in scraped_data:
            map_article(article, user_query)

       
        return jsonify({"message": "success", "data": scraped_data}), 200

    except Exception as e:
        print(f"❌ Server Error: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

import json

def map_article(article, query):
    """
    Store articles under each query in a Redis hash per user:
    Key: user:{user_id}:queries
    Field: query
    Value: JSON list of article URLs
    """

    url = article['url']
    session_id = request.cookies.get('SESSION_ID')
    user_id = aqd_object.get_userID_from_session(SESSION_ID=session_id)

    if user_id is None:
        return

    redis_key = f"user:{user_id}:queries"

    # Fetch existing list of URLs for the query
    existing_data = aqd_object.redis_usr.hget(redis_key, query)
    if existing_data:
        url_list = json.loads(existing_data)
    else:
        url_list = []

    # Only add URL if it's not already in the list
    if url not in url_list:
        url_list.append(url)
        aqd_object.redis_usr.hset(redis_key, query, json.dumps(url_list))
        aqd_object.redis_usr.expire(redis_key, 3600)  # 1 hour TTL


@app.route('/api/save', methods=['POST'])
@log_event(service_name=BACKEND_SERVICE_NAME, event_base="SaveArticle")
def save():
    try:
        aqd_object.db.connect()
        data = request.get_json()
        session_id = request.cookies.get('SESSION_ID')

        if not session_id:
            return jsonify({"error": "Session not found or expired."}), 401


        user_id = aqd_object.get_userID_from_session(SESSION_ID=session_id)
        if user_id is None:
            return jsonify({"error": "Unauthorized – No userId in session"}), 401
        
        # ✅ Validate input
        if not data or "url" not in data:
            return jsonify({"error": "Invalid input, 'url' is required"}), 400

        urls = data["url"]

        # ✅ Ensure 'urls' is always a list
        if isinstance(urls, str):
            urls = [urls]  # Convert single string to list

        for url in urls:
            aqd_object.move_article_to_database(url, user_id)  # ✅ Corrected usage

        return jsonify({"message": "success"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
@app.route('/api/get_up_vote', methods=['POST'])
@log_event(service_name=BACKEND_SERVICE_NAME, event_base="GetUpVote")
def get_up_vote():
    data = request.get_json()
    url = data.get('url')
    try:

        session_id = request.cookies.get('SESSION_ID')

        if not session_id:
            return jsonify({"error": "Please log in to continue"}), 401
        
        user_id=aqd_object.get_userID_from_session(SESSION_ID=session_id)
        if user_id is None:
            return jsonify({"Guest cannot vote on the article"})

        user_votes_key = f"user:{user_id}:personal_vote"
        vote_type = aqd_object.redis_usr.hget(user_votes_key, url) or str(NEUTRAL_VOTE)
        vote_type= int(vote_type)

        redis_data = aqd_object.redis_client.get(url)

        if redis_data is None:
            article_data = {"upvotes": NEUTRAL_VOTE}
        else:
            # Parse the JSON string into a Python dictionary
            article_data = json.loads(redis_data.decode("utf-8"))

        #neutral vote to upvote +1
        if vote_type==NEUTRAL_VOTE:
            article_data["upvotes"] += 1
            aqd_object.redis_usr.hset(user_votes_key, url, UP_VOTE)
        #upvote to neutral vote -1
        if vote_type == UP_VOTE:
            aqd_object.redis_client.hincrby(url, 'up_vote', -1)
            aqd_object.redis_usr.hset(user_votes_key, url, NEUTRAL_VOTE)
        #downvote to upvote +2
        elif vote_type == DOWN_VOTE:
            article_data["upvotes"] += 2
            aqd_object.redis_usr.hset(user_votes_key, url, UP_VOTE)
            vote_type = UP_VOTE
            
        aqd_object.redis_client.set(url, json.dumps(article_data))
        return flask.jsonify({'vote_type': vote_type}), 200
    except Exception as e:
        return flask.jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/get_down_vote', methods=['POST'])
@log_event(service_name=BACKEND_SERVICE_NAME, event_base="GetDownVote")
def get_down_vote():
    data = request.get_json()
    url = data.get('url')
    try:

        session_id = request.cookies.get('SESSION_ID')

        if not session_id:
            return jsonify({"error": "Guest cannot vote on the article. Please log in to continue"}), 401
        

        user_id=aqd_object.get_userID_from_session(SESSION_ID=request.cookies.get('SESSION_ID'))
        if user_id is None:
            return jsonify({ "User not found in session"})

        user_votes_key = f"user:{user_id}:personal_vote"
        vote_type = aqd_object.redis_usr.hget(user_votes_key, url) or int(NEUTRAL_VOTE)
        vote_type= int(vote_type)

        redis_data = aqd_object.redis_client.get(url)
        if redis_data is None:
            article_data = {"upvotes": NEUTRAL_VOTE}
        else:
            # Parse the JSON string into a Python dictionary
            article_data = json.loads(redis_data.decode("utf-8"))


        #neutral vote to downvote -1
        if vote_type==NEUTRAL_VOTE:
            article_data["upvotes"] += -1
            aqd_object.redis_usr.hset(user_votes_key, url, DOWN_VOTE)
        #downvote to neutral vote +1
        if vote_type == DOWN_VOTE:
            aqd_object.redis_client.hincrby(url, 'down_vote', 1)
            aqd_object.redis_usr.hset(user_votes_key, url, NEUTRAL_VOTE)
        #upvote to downvote -2
        elif vote_type == UP_VOTE:
            article_data["upvotes"] += -2
            aqd_object.redis_usr.hset(user_votes_key, url, DOWN_VOTE)
            vote_type = DOWN_VOTE
            
        aqd_object.redis_client.set(url, json.dumps(article_data))
        return flask.jsonify({'vote_type': vote_type}), 200
    except Exception as e:
        return flask.jsonify({'status': 'error', 'message': str(e)}), 500




# if __name__ == "__main__":
#     print("Starting Flask app on port 7001...")
#     app.run(debug=True, host="0.0.0.0", port=7001)  
