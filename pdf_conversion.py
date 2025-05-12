import PyPDF2
import os
import sys


def convert_pdf_to_txt(pdf_path, output_dir):
    try:

        os.makedirs(output_dir, exist_ok=True)

        with open(pdf_path, 'rb') as file:

            pdf_reader = PyPDF2.PdfReader(file)

            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
            output_path = os.path.join(output_dir, f"{base_name}.txt")

            with open(output_path, 'w', encoding='utf-8') as txt_file:

                for page in pdf_reader.pages:
                    text = page.extract_text()
                    if text:
                        txt_file.write(text)
                        txt_file.write('\n')

            print(f"Successfully converted {pdf_path} to {output_path}")

    except Exception as e:
        print(f"Error processing {pdf_path}: {str(e)}")


def main():

    if len(sys.argv) < 2:
        print("Usage: python pdf_to_txt.py <pdf_file_or_directory> [output_directory]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "txt_output"

    if os.path.isdir(input_path):
        for filename in os.listdir(input_path):
            if filename.lower().endswith('.pdf'):
                pdf_path = os.path.join(input_path, filename)
                convert_pdf_to_txt(pdf_path, output_dir)
    elif os.path.isfile(input_path) and input_path.lower().endswith('.pdf'):
        convert_pdf_to_txt(input_path, output_dir)
    else:
        print("Error: Input must be a PDF file or directory containing PDF files")
        sys.exit(1)


if __name__ == "__main__":
    main()