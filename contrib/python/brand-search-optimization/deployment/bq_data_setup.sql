-- Create the table in BigQuery
CREATE TABLE IF NOT EXISTS your_project_id.your_dataset_id.products (
    Title STRING,
    Description STRING,
    Attributes STRING,
    Brand STRING
);
-- Notes:
-- - Replace your_project_id with your Google Cloud Project ID.
-- - Replace your_dataset_id with the name of your BigQuery Dataset.

-- Insert data into the table
INSERT INTO your_project_id.your_dataset_id.products
            (Title,
            Description,
            Attributes,
            Brand)
VALUES      ('Kids\' Joggers',
'Comfortable and supportive running shoes for active kids. Breathable mesh upper keeps feet cool, while the durable outsole provides excellent traction.'
            ,
'Size: 10 Toddler, Color: Blue/Green',
'BSOAgentTestBrand'),
            ('Light-Up Sneakers',
'Fun and stylish sneakers with light-up features that kids will love. Supportive and comfortable for all-day play.'
            ,
'Size: 13 Toddler, Color: Silver',
'BSOAgentTestBrand'),
            ('School Shoes',
'Versatile and comfortable shoes perfect for everyday wear at school. Durable construction with a supportive design.'
            ,
'Size: 12 Preschool, Color: Black',
'BSOAgentTestBrand');
-- Notes:
-- - Ensure the project and dataset IDs match the ones used in the CREATE TABLE statement.