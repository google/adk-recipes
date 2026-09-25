import json
import os
import re
import uuid
import json
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from dotenv import load_dotenv
import warnings
import logging

warnings.filterwarnings("ignore")

load_dotenv()

logger = logging.getLogger(__name__)

def search_asset_bank(query: str) -> str:
    # Helper to clean text and split CamelCase (e.g., 'TrueBlue' -> 'true blue')
    def preprocess(text):
        # Insert space before capital letters if they are preceded by a lowercase letter
        text = re.sub(r'(?<!^)(?=[A-Z])', ' ', text)
        return text.lower()

    # Prepare document strings from the JSON data
    # We combine name, description, and color for the best context
    # Assets can be stored in GCS or any other storage system like Digital Assets Management (DAM)
    # For now, we are using a local JSON file for demonstration purposes
    dataset_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'data', 'brand_assets_metadata.json')
    dataset = json.load(open(dataset_path))
    documents = []
    ids = []
    for item in dataset:
        # Create a rich text representation of the item
        doc_text = f"{item['name']} {item['description']} {item['primary_subject_color_name']} {item['primary_subject_type']}"
        documents.append(preprocess(doc_text))
        ids.append(item['id'])

    # Preprocess the query
    processed_query = preprocess(query)

    # Use TF-IDF Vectorizer
    vectorizer = TfidfVectorizer()
    
    # Fit on documents and transform both documents and query
    tfidf_matrix = vectorizer.fit_transform(documents + [processed_query])
    
    # Calculate Cosine Similarity between the query (last vector) and all documents
    cosine_sim = cosine_similarity(tfidf_matrix[-1], tfidf_matrix[:-1])
    
    # Find the index of the highest score
    best_match_index = cosine_sim.argmax()
    best_score = cosine_sim[0, best_match_index]


    logger.info(f"Best match found: {dataset[best_match_index]}")
    
    return dataset[best_match_index]

