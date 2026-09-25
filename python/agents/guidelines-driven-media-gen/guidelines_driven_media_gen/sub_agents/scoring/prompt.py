SCORING_PROMPT = """

      "Your task is to evaluate an image based on a set of scoring rules. Follow these steps precisely:"
        "1.  First, invoke the async 'get_image' tool to load the images artifact and image_metadata. Do not try to generate the image."\
        " Wait for the image to be loaded and the response. CRITICAL: If no image artifact was loaded or if the tool returns an error, you MUST immediately return a total_score of 0 with a JSON indicating failure, invoke the set_score tool with 0, and stop evaluation."\
        "2.  Next, invoke the 'get_policy' tool to obtain the image scoring 'rules' in JSON format (do this ONLY if an image was successfully loaded in step 1)."
        "3.  Scoring Criteria: Carefully examine the rules in JSON string obtained in step 1. \
             Each rule in the policy not only applies to primary subject of the image \
            but to all content in the image, including backgrounds. Getting the branding right is of utmost importance.\
            For EACH rule described within this JSON string:"
        "    a.  Strictly score the loaded image (from step 2) against each criterion mentioned in the JSON string."
        "    b.  Assign a score in a scale of 0 to 5: 5 points if the image complies with a specific criterion, or 0 point if it does not." \
             "Also specify the reason in a separate attribute explaining the reason for assigning thew score"
        "4. An example of the computed scoring criteria is as follows: "
        "{\
          \"total_score\": 50,\
          \"scores\": {\
            \"General Guidelines\": {\
              \"score\": 5,\
              \"reason\": \"The image conforms to the empathetic and innovative brand voice of NeuroVibe AI.\"\
            },\
            \"Primary Color Palette\": {\
              \"score\": 5,\
              \"reason\": \"The image correctly utilizes Neuro Blue for dark backgrounds and Cognitive White for standard backgrounds.\"\
            },\
            \"Accent Color Palette\": {\
              \"score\": 5,\
              \"reason\": \"The image uses Vibe Electric accurately for the secondary logo without mentioning the color names.\"\
            },\
            \"Logo Specifications - Primary Logo\": {\
              \"score\": 5,\
              \"reason\": \"The primary logo lockup is clear and legible.\"\
            },\
            \"Logo Specifications - Secondary Logo\": {\
              \"score\": 5,\
              \"reason\": \"The Pulse Arrow is used appropriately as a standalone icon.\"\
            },\
            \"Brand Prohibitions (Strict Don'ts)\": {\
              \"score\": 5,\
              \"reason\": \"The image does not violate any brand prohibitions such as stretching or altering the logo.\"\
            },\
            \"Logo placement Restrictions\": {\
              \"score\": 0,\
              \"reason\": \"The clear space around the logo is cluttered with text, which is a violation.\"\
            },\
            \"Image Specifications and Guidelines\": {\
              \"score\": 5,\
              \"reason\": \"The primary subjects are shown in full and not cropped.\"\
            },\
            \"Text Specifications and Guidelines\": {\
              \"score\": 5,\
              \"reason\": \"The text utilizes Helvetica-Bold for headings appropriately.\"\
            },\
            \"Safe Zones\": {\
              \"score\": 5,\
              \"reason\": \"The image respects all safe zones around important content.\"\
            },\
            \"Composition Styles\": {\
              \"score\": 5,\
              \"reason\": \"The image follows the composition styles ensuring high contrast.\"\
            },\
            \"Trade Mark Specifications\": {\
              \"score\": 0,\
              \"reason\": \"The image improperly includes a trade mark symbol next to the primary logo.\"\
            }\
          }\
        }"


        "Do not validate the JSON structure itself; only use its content for scoring rules. "
        "5. Compute the total_score by adding each individual score point for each rule in the JSON "
        "6. Invoke the set_score tool and pass the total_score. "

       
        "OUTPUT JSON FORMAT SPECIFICATION:\n"
        "The JSON object MUST have exactly two top-level keys:"
        "  - 'total_score': Iterate through each individual score element in the json and add those to arrive at total_score. "
        "  - 'scores': The existing rules json with a score attribute assigned to each rule and a reason attribute"
  
"""
