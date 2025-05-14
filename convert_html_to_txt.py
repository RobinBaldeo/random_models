import os
import html2text
import argparse

def convert_html_to_txt(input_folder, output_folder):
    """Convert all HTML files in input_folder to TXT files in output_folder."""
    # Validate input folder
    if not os.path.isdir(input_folder):
        print(f"Error: '{input_folder}' is not a valid directory.")
        return False

    # Create output folder if it doesn't exist
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # Initialize html2text converter
    h = html2text.HTML2Text()
    h.ignore_links = True  # Ignore links for cleaner output
    h.ignore_images = True  # Ignore images
    h.body_width = 0  # Disable line wrapping for plain text

    # Get list of HTML files in the input folder
    html_files = [f for f in os.listdir(input_folder) if f.endswith(('.html', '.htm'))]

    # Check if there are any HTML files
    if not html_files:
        print(f"No HTML files found in '{input_folder}'.")
        return True

    # Process each HTML file
    for html_file in html_files:
        try:
            # Read HTML file
            input_path = os.path.join(input_folder, html_file)
            with open(input_path, 'r', encoding='utf-8') as file:
                html_content = file.read()

            # Convert HTML to plain text
            text = h.handle(html_content)

            # Define output TXT file path
            output_filename = os.path.splitext(html_file)[0] + '.txt'
            output_path = os.path.join(output_folder, output_filename)

            # Save to TXT file
            with open(output_path, 'w', encoding='utf-8') as file:
                file.write(text)

            print(f"Converted {html_file} to {output_filename}")

        except Exception as e:
            print(f"Error processing {html_file}: {str(e)}")

    print("Conversion complete!")
    return True

if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Convert HTML files in a folder to TXT files.")
    parser.add_argument("html_path", help="Path to the folder containing HTML files")
    parser.add_argument("txt_path", help="Path to the output folder for TXT files")
    args = parser.parse_args()

    # Run conversion
    convert_html_to_txt(args.html_path, args.txt_path)