import os
import logging

# Set up logging to output to the console
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


def dump_files():
    input_dir = './files'
    output_file = 'content.txt'

    try:
        # Open the output file in write mode
        with open(output_file, 'w', encoding='utf-8') as outfile:

            # Iterate through the files directory
            for filename in os.listdir(input_dir):
                if filename.endswith('.py'):
                    file_path = os.path.join(input_dir, filename)

                    try:
                        # Read the python file and append its contents
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(f"\n# --- BEGIN {filename} ---\n\n")
                            outfile.write(infile.read())
                            outfile.write(f"\n\n# --- END {filename} ---\n")

                        logging.info(f"Successfully processed: {filename}")

                    except Exception as e:
                        logging.error(f"Failed to read {filename}: {e}")

        logging.info(f"Process complete! All content saved to {output_file}")

    except FileNotFoundError:
        logging.error(f"Directory not found: {input_dir}. Please ensure it exists.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")


if __name__ == '__main__':
    dump_files()