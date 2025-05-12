import yaml
import argparse
from types import SimpleNamespace
import sys, os, re
import pdb
import glob
from pathlib import Path
import polars as pl


def dict_to_obj(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: dict_to_obj(v) for k, v in d.items()})
    elif isinstance(d, list):
        return [dict_to_obj(item) for item in d]
    return d


def read_txt_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        return content
    except Exception as e:
        print(f"Error reading {file_path}: {str(e)}")
        return ''


def read_txt_file_path(file_path):
    txt_dict = {}
    for i in Path(config.data.policy_path).glob("*.txt"):
        file_path = re.sub(".txt$", "", os.path.basename(i))
        txt_dict[file_path] = read_txt_file(i)
    return txt_dict


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the YAML configuration file"
    )

    args = parser.parse_args()
    experiment_name = re.sub(".yaml$", "", os.path.basename(args.config))
    print(experiment_name)

    try:
        with open(args.config, 'r') as file:
            config_dict = yaml.safe_load(file)
    except FileNotFoundError:
        print(f"Error: Config file '{args.config}' not found")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML: {e}")
        sys.exit(1)

    config = dict_to_obj(config_dict)
    policy_path_dic = read_txt_file_path(config.data.policy_path)

    sample_questions_df = (
        pl.read_excel(config.data.sample_questions)
        .group_by('Policy')
        .agg(
            Policy_Questions=pl.col('Question')
        )
        .with_columns(
            Policy_txt=pl.col('Policy').map_elements(lambda g: policy_path_dic.get(g, "")),
            Sample_Questions=pl.col('Policy_Questions').list.join('|'),
            keyword=pl.read_excel(config.data.key_word_path)['top_words'].to_list()
        )
        .with_columns(
            keyword=pl.col('keyword').list.join('|')
        )
        .with_columns(
            system=pl.struct(['Policy_txt', 'Sample_Questions', 'keyword']).map_elements(
                lambda struct: config.prompt.system_role.format(
                    persona=config.prompt.persona,
                    num_questions=config.prompt.generated_questions,
                    key_phrases=struct['keyword'],
                    policy_text=struct['Policy_txt'],
                    sample_questions=struct['Sample_Questions']
                )
            ),
            user=pl.struct(['Sample_Questions', 'keyword']).map_elements(
                lambda struct: config.prompt.user_role.format(
                    persona=config.prompt.persona,
                    num_questions=config.prompt.generated_questions,
                    key_phrases=struct['keyword'] if struct['keyword'] else "None",
                    sample_questions=struct['Sample_Questions']
                )
            )
        )
        )


    print(sample_questions_df.head(5))
    print(sample_questions_df['system'].to_list()[0])
    # print(config.prompt.base_prompt.format(placeholder="cat"))

