# Customization

## Instructions for Adding Permissions to the BigQuery Table

This document provides instructions on how to grant users permissions to access the BigQuery table created and populated by the `brand_search_optimization/bq_populate_data.py` script. You can use the Google Cloud Console, the `bq` command-line tool, or the BigQuery API to manage these permissions.

### 1. Using the Google Cloud Console:

1.  Go to the Google Cloud Console: [https://console.cloud.google.com/bigquery](https://console.cloud.google.com/bigquery)
2.  In the Explorer panel (left-hand side), expand your project and then your dataset.
3.  Click on the name of your table.
4.  On the Table Details page, click on the "Permissions" tab.
5.  Click the "Add Principal" button.
6.  In the "New principals" field, enter the email address or identifier of the user (or group/service account) you want to grant permissions to.
7.  In the "Select a role" dropdown, choose the appropriate role. Some common roles for accessing BigQuery tables include:
    * **BigQuery Data Viewer:** Allows the user to query and view table data.
    * **BigQuery Data Editor:** Allows the user to modify table data (insert, update, delete).
    * **BigQuery Admin:** Provides full control over the table.
    * **BigQuery User:** Grants basic permissions to run queries and perform other BigQuery actions within the project.
8.  Click "Save".

### 2. Using the `bq` Command-Line Tool:

1.  Make sure you have the Google Cloud CLI (`gcloud`) installed and configured.
2.  Open your terminal or command prompt.
3.  Use the `bq add-iam-policy` command with the appropriate role and member.

    For example, to grant the role of BigQuery Data Viewer to the user `user@example.com` on your table (assuming your project ID is `your-project-id`, your dataset ID is `your_dataset_id`, and your table ID is `products`):

    ```bash
    bq add-iam-policy --member=user:user@example.com --role=roles/bigquery.dataViewer your-project-id:your_dataset_id.products
    ```

    Replace:
    * `your-project-id` with your actual Google Cloud Project ID.
    * `your_dataset_id` with the name of your BigQuery Dataset.
    * `products` with the name of your BigQuery Table (if you changed the default).

    You can find a list of available roles in the BigQuery documentation.

### 3. Using the BigQuery API (Programmatically):

You can use the BigQuery API in your Python script or other programming languages to manage table permissions. This involves interacting with the `Table` resource and its `iamPolicy` property. Here's a basic Python example using the `google-cloud-bigquery` library:

```python
from google.cloud import bigquery

# Replace with your actual details
PROJECT_ID = "your-project-id"
DATASET_ID = "your_dataset_id"
TABLE_ID = "products"
USER_EMAIL = "user@example.com"
ROLE = "roles/bigquery.dataViewer"

client = bigquery.Client(project=PROJECT_ID)
table_ref = client.dataset(DATASET_ID).table(TABLE_ID)
table = client.get_table(table_ref)  # Get the current table metadata

policy = table.iam_policy
policy.bindings.append({"role": ROLE, "members": [f"user:{USER_EMAIL}"]})
table.iam_policy = policy

table = client.update_table(table)  # Updates the table's IAM policy

print(
    f"Granted role '{ROLE}' to user '{USER_EMAIL}' on table '{table.full_table_id}'."
)
```