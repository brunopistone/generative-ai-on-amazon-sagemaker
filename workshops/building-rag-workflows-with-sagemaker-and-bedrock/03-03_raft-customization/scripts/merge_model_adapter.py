import logging
import os
import random
import torch
from transformers import AutoTokenizer, TrainingArguments
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    set_seed,
)
from typing import Dict, Optional, Tuple
import argparse
from datasets import load_dataset
from pprint import pprint

import subprocess as sb

def set_custom_env(env_vars: Dict[str, str]) -> None:
    """
    Set custom environment variables.

    Args:
        env_vars (Dict[str, str]): A dictionary of environment variables to set.
                                   Keys are variable names, values are their corresponding values.

    Returns:
        None

    Raises:
        TypeError: If env_vars is not a dictionary.
        ValueError: If any key or value in env_vars is not a string.
    """
    if not isinstance(env_vars, dict):
        raise TypeError("env_vars must be a dictionary")

    for key, value in env_vars.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("All keys and values in env_vars must be strings")

    os.environ.update(env_vars)

    # Optionally, print the updated environment variables
    print("Updated environment variables:")
    for key, value in env_vars.items():
        print(f"  {key}: {value}")
        
def create_test_prompt():
    dataset = load_dataset(
        "json",
        #data_files=os.path.join(args.testdata, "dataset.json"),
        data_dir=args.testdata,
        split="test"
    )
    
    # Shuffle the dataset and select the first row
    random_row = dataset.shuffle().select(range(1))[0]
    
    return random_row
    
# Generate in-memory inference
def generate_text(model, prompt, max_length=2048, num_return_sequences=1):
    # Encode the input prompt
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    #model = model.to(device)
        
    tokenizer = AutoTokenizer.from_pretrained(
            args.basemodel if args.use_local else args.model_id,
            use_fast=True
        )
    
    tokenizer.pad_token = tokenizer.eos_token
    
    tokenizer.save_pretrained("/opt/ml/model/merged/")
    
    prompt_input=prompt['prompt'].split("### Summary")[0]
    
    input_ids = tokenizer.encode(prompt_input, return_tensors="pt")#.to(device)

    # Generate text
    with torch.no_grad():
        output = model.generate(
            input_ids,
            max_length=max_length,
            num_return_sequences=num_return_sequences,
            no_repeat_ngram_size=2,
            top_k=50,
            top_p=0.95,
            temperature=0.7
        )

    # Decode and return the generated text
    generated_texts = [tokenizer.decode(seq, skip_special_tokens=True) for seq in output]
        
    return generated_texts

# Merge the trained adapter with the base model and test it
def merge_and_save_model(model_id, adapter_dir, output_dir):
    from peft import PeftModel

    ##################
    # Load Base Model
    ##################
    print("Trying to load a Peft model. It might take a while without feedback")
    base_model = AutoModelForCausalLM.from_pretrained(
        args.basemodel if args.use_local else model_id,
        low_cpu_mem_usage=True,
        torch_dtype=torch.float32,
        device_map="auto",
      #  offload_folder="/opt/ml/model/"
    )
    
    print("Loaded base model")
    
    #############################
    # Run Inference - Base Model 
    #############################
    prompt=create_test_prompt()
    
    #pprint(f"*** Generating Inference on Base Model: {generate_text(base_model,prompt)}")
    
    base_model.config.use_cache = False
    
    ################
    # Load Adapter
    ################
    # Load the adapter
    peft_model = PeftModel.from_pretrained(
        base_model, 
        adapter_dir, 
        torch_dtype=torch.float32,  # Set dtype to float16
     #   offload_folder="/opt/ml/model/"
    )
    
    ###############################
    # Merge Adapter and Base Model
    ###############################
    print("Loaded peft model")
    model = peft_model.merge_and_unload()
    print("Merge done")

    model.eval()
    model.active_adapters = "default" 
    #############################
    # Run Inference - Trained Model 
    #############################
    pprint(f"*** Generating Inference on Trained Model: {generate_text(model,prompt)}")

    os.makedirs(output_dir, exist_ok=True)
    
    ##################################
    # Save Merged Model and Tokenizer
    ##################################
    print(f"Saving the newly created merged model to {output_dir}")
    model.save_pretrained(output_dir, safe_serialization=True)
    base_model.config.save_pretrained(output_dir)

# Parse CLI arguments passed by SageMaker Jobs
def parse_arge():

    parser = argparse.ArgumentParser()

    # infra configuration
    parser.add_argument("--adapterdir", type=str, default=os.environ["SM_CHANNEL_ADAPTER"])
    parser.add_argument("--testdata", type=str, default=os.environ["SM_CHANNEL_TESTDATA"])
    
    parser.add_argument("--basemodel", type=str, default=os.environ.get("SM_CHANNEL_BASEMODEL",""))
    parser.add_argument('--use_local', type=lambda x: str(x).lower() in ['true', '1', 't', 'y', 'yes'], help="A boolean flag")
    
    parser.add_argument("--model_id", type=str, default="meta-llama/Meta-Llama-3.1-8B")
    parser.add_argument("--hf_token", type=str, default="")
    parser.add_argument("--dataset_name", type=str, default="")
    
    args = parser.parse_known_args()
    
    return args

if __name__ == "__main__":    
    
    args, _ = parse_arge()
    
    custom_env: Dict[str, str] = {"HF_DATASETS_TRUST_REMOTE_CODE": "TRUE",
                                  "HF_TOKEN": args.hf_token
                                  }
    set_custom_env(custom_env)

    print("*****printing adapetrs")

    # Run the command to get the Adapter artifacts
    sb.run(["ls", "-ltr", args.adapterdir])

    # launch training to merge trained adapaters with base model
    merge_and_save_model(args.model_id, args.adapterdir,"/opt/ml/model/merged/")
