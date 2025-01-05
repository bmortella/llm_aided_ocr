import os
import traceback
import asyncio
import re
import logging
import warnings
from typing import List, Tuple, Optional
import tiktoken
from decouple import Config as DecoupleConfig, RepositoryEnv
from transformers import AutoTokenizer
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
import ollama
from paddleocr import PaddleOCR

# Configuration
config = DecoupleConfig(RepositoryEnv('.env'))

USE_LOCAL_LLM = config.get("USE_LOCAL_LLM", default=False, cast=bool)
API_PROVIDER = config.get("API_PROVIDER", default="OPENAI", cast=str) # OPENAI or CLAUDE
ANTHROPIC_API_KEY = config.get("ANTHROPIC_API_KEY", default="your-anthropic-api-key", cast=str)
OPENAI_API_KEY = config.get("OPENAI_API_KEY", default="your-openai-api-key", cast=str)
CLAUDE_MODEL_STRING = config.get("CLAUDE_MODEL_STRING", default="claude-3-haiku-20240307", cast=str)
CLAUDE_MAX_TOKENS = 4096 # Maximum allowed tokens for Claude API
TOKEN_BUFFER = 500  # Buffer to account for token estimation inaccuracies
TOKEN_CUSHION = 300 # Don't use the full max tokens to avoid hitting the limit
OPENAI_COMPLETION_MODEL = config.get("OPENAI_COMPLETION_MODEL", default="gpt-4o-mini", cast=str)
OPENAI_EMBEDDING_MODEL = config.get("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small", cast=str)
OPENAI_MAX_TOKENS = 12000  # Maximum allowed tokens for OpenAI API
DEFAULT_LOCAL_MODEL_NAME = "llama3.1"
LOCAL_LLM_CONTEXT_SIZE_IN_TOKENS = 2048
USE_VERBOSE = False

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
warnings.filterwarnings("ignore", category=FutureWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Model Download
def download_model():
    resp = ollama.pull(DEFAULT_LOCAL_MODEL_NAME)
    return resp

# API Interaction Functions
async def generate_completion(prompt: str, max_tokens: int = 5000) -> Optional[str]:
    if USE_LOCAL_LLM:
        return await generate_completion_from_local_llm(DEFAULT_LOCAL_MODEL_NAME, prompt, max_tokens)
    elif API_PROVIDER == "CLAUDE":
        return await generate_completion_from_claude(prompt, max_tokens)
    elif API_PROVIDER == "OPENAI":
        return await generate_completion_from_openai(prompt, max_tokens)
    else:
        logging.error(f"Invalid API_PROVIDER: {API_PROVIDER}")
        return None

def get_tokenizer(model_name: str):
    if model_name.lower().startswith("gpt-"):
        return tiktoken.encoding_for_model(model_name)
    elif model_name.lower().startswith("claude-"):
        return AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b", clean_up_tokenization_spaces=False)
    elif model_name.lower().startswith("llama"):
        return AutoTokenizer.from_pretrained("huggyllama/llama-7b", clean_up_tokenization_spaces=False)
    else:
        raise ValueError(f"Unsupported model: {model_name}")

def estimate_tokens(text: str, model_name: str) -> int:
    try:
        tokenizer = get_tokenizer(model_name)
        return len(tokenizer.encode(text))
    except Exception as e:
        logging.warning(f"Error using tokenizer for {model_name}: {e}. Falling back to approximation.")
        return approximate_tokens(text)

def approximate_tokens(text: str) -> int:
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text.strip())
    # Split on whitespace and punctuation, keeping punctuation
    tokens = re.findall(r'\b\w+\b|\S', text)
    count = 0
    for token in tokens:
        if token.isdigit():
            count += max(1, len(token) // 2)  # Numbers often tokenize to multiple tokens
        elif re.match(r'^[A-Z]{2,}$', token):  # Acronyms
            count += len(token)
        elif re.search(r'[^\w\s]', token):  # Punctuation and special characters
            count += 1
        elif len(token) > 10:  # Long words often split into multiple tokens
            count += len(token) // 4 + 1
        else:
            count += 1
    # Add a 10% buffer for potential underestimation
    return int(count * 1.1)

def chunk_text(text: str, max_chunk_tokens: int, model_name: str) -> List[str]:
    chunks = []
    tokenizer = get_tokenizer(model_name)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    current_chunk = []
    current_chunk_tokens = 0
    
    for sentence in sentences:
        sentence_tokens = len(tokenizer.encode(sentence))
        if current_chunk_tokens + sentence_tokens > max_chunk_tokens:
            chunks.append(' '.join(current_chunk))
            current_chunk = [sentence]
            current_chunk_tokens = sentence_tokens
        else:
            current_chunk.append(sentence)
            current_chunk_tokens += sentence_tokens
    
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    
    adjusted_chunks = adjust_overlaps(chunks, tokenizer, max_chunk_tokens)
    return adjusted_chunks

def split_long_sentence(sentence: str, max_tokens: int, model_name: str) -> List[str]:
    words = sentence.split()
    chunks = []
    current_chunk = []
    current_chunk_tokens = 0
    tokenizer = get_tokenizer(model_name)
    
    for word in words:
        word_tokens = len(tokenizer.encode(word))
        if current_chunk_tokens + word_tokens > max_tokens and current_chunk:
            chunks.append(' '.join(current_chunk))
            current_chunk = [word]
            current_chunk_tokens = word_tokens
        else:
            current_chunk.append(word)
            current_chunk_tokens += word_tokens
    
    if current_chunk:
        chunks.append(' '.join(current_chunk))
    
    return chunks

def adjust_overlaps(chunks: List[str], tokenizer, max_chunk_tokens: int, overlap_size: int = 50) -> List[str]:
    adjusted_chunks = []
    for i in range(len(chunks)):
        if i == 0:
            adjusted_chunks.append(chunks[i])
        else:
            overlap_tokens = len(tokenizer.encode(' '.join(chunks[i-1].split()[-overlap_size:])))
            current_tokens = len(tokenizer.encode(chunks[i]))
            if overlap_tokens + current_tokens > max_chunk_tokens:
                overlap_adjusted = chunks[i].split()[:-overlap_size]
                adjusted_chunks.append(' '.join(overlap_adjusted))
            else:
                adjusted_chunks.append(' '.join(chunks[i-1].split()[-overlap_size:] + chunks[i].split()))
    
    return adjusted_chunks

async def generate_completion_from_claude(prompt: str, max_tokens: int = CLAUDE_MAX_TOKENS - TOKEN_BUFFER) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        logging.error("Anthropic API key not found. Please set the ANTHROPIC_API_KEY environment variable.")
        return None
    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    prompt_tokens = estimate_tokens(prompt, CLAUDE_MODEL_STRING)
    adjusted_max_tokens = min(max_tokens, CLAUDE_MAX_TOKENS - prompt_tokens - TOKEN_BUFFER)
    if adjusted_max_tokens <= 0:
        logging.warning("Prompt is too long for Claude API. Chunking the input.")
        chunks = chunk_text(prompt, CLAUDE_MAX_TOKENS - TOKEN_CUSHION, CLAUDE_MODEL_STRING)
        results = []
        for chunk in chunks:
            try:
                async with client.messages.stream(
                    model=CLAUDE_MODEL_STRING,
                    max_tokens=CLAUDE_MAX_TOKENS // 2,
                    temperature=0.7,
                    messages=[{"role": "user", "content": chunk}],
                ) as stream:
                    message = await stream.get_final_message()
                    results.append(message.content[0].text)
                    logging.info(f"Chunk processed. Input tokens: {message.usage.input_tokens:,}, Output tokens: {message.usage.output_tokens:,}")
            except Exception as e:
                logging.error(f"An error occurred while processing a chunk: {e}")
        return " ".join(results)
    else:
        try:
            async with client.messages.stream(
                model=CLAUDE_MODEL_STRING,
                max_tokens=adjusted_max_tokens,
                temperature=0.7,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = await stream.get_final_message()
                output_text = message.content[0].text
                logging.info(f"Total input tokens: {message.usage.input_tokens:,}")
                logging.info(f"Total output tokens: {message.usage.output_tokens:,}")
                logging.info(f"Generated output (abbreviated): {output_text[:150]}...")
                return output_text
        except Exception as e:
            logging.error(f"An error occurred while requesting from Claude API: {e}")
            return None

async def generate_completion_from_openai(prompt: str, max_tokens: int = 5000) -> Optional[str]:
    if not OPENAI_API_KEY:
        logging.error("OpenAI API key not found. Please set the OPENAI_API_KEY environment variable.")
        return None
    prompt_tokens = estimate_tokens(prompt, OPENAI_COMPLETION_MODEL)
    adjusted_max_tokens = min(max_tokens, 4096 - prompt_tokens - TOKEN_BUFFER)  # 4096 is typical max for GPT-3.5 and GPT-4
    if adjusted_max_tokens <= 0:
        logging.warning("Prompt is too long for OpenAI API. Chunking the input.")
        chunks = chunk_text(prompt, OPENAI_MAX_TOKENS - TOKEN_CUSHION, OPENAI_COMPLETION_MODEL) 
        results = []
        for chunk in chunks:
            try:
                response = await openai_client.chat.completions.create(
                    model=OPENAI_COMPLETION_MODEL,
                    messages=[{"role": "user", "content": chunk}],
                    max_tokens=adjusted_max_tokens,
                    temperature=0.7,
                )
                result = response.choices[0].message.content
                results.append(result)
                logging.info(f"Chunk processed. Output tokens: {response.usage.completion_tokens:,}")
            except Exception as e:
                logging.error(f"An error occurred while processing a chunk: {e}")
        return " ".join(results)
    else:
        try:
            response = await openai_client.chat.completions.create(
                model=OPENAI_COMPLETION_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=adjusted_max_tokens,
                temperature=0.7,
            )
            output_text = response.choices[0].message.content
            logging.info(f"Total tokens: {response.usage.total_tokens:,}")
            logging.info(f"Generated output (abbreviated): {output_text[:150]}...")
            return output_text
        except Exception as e:
            logging.error(f"An error occurred while requesting from OpenAI API: {e}")
            return None

async def generate_completion_from_local_llm(llm_model_name: str, input_prompt: str, number_of_tokens_to_generate: int = 100, temperature: float = 0.7):
    logging.info(f"Starting text completion using model: '{llm_model_name}' for input prompt: '{input_prompt}'")
    prompt_tokens = estimate_tokens(input_prompt, llm_model_name)
    adjusted_max_tokens = min(number_of_tokens_to_generate, LOCAL_LLM_CONTEXT_SIZE_IN_TOKENS - prompt_tokens - TOKEN_BUFFER)
    if adjusted_max_tokens <= 0:
        logging.warning("Prompt is too long for LLM. Chunking the input.")
        chunks = chunk_text(input_prompt, LOCAL_LLM_CONTEXT_SIZE_IN_TOKENS - TOKEN_CUSHION, llm_model_name)
        results = []
        for chunk in chunks:
            try:
                output = ollama.generate(
                    model=llm_model_name,
                    prompt=chunk,
                    options= {
                        "num_predict": LOCAL_LLM_CONTEXT_SIZE_IN_TOKENS - TOKEN_CUSHION,
                        "temperature": temperature
                    },
                )
                results.append(output['response'])
                logging.info(f"Chunk processed. Output tokens: {output['eval_count']}")
            except Exception as e:
                logging.error(f"An error occurred while processing a chunk: {e}")
        return " ".join(results)
    else:
        output = ollama.generate(
            model=llm_model_name,
            prompt=input_prompt,
            options={
                "num_predict": adjusted_max_tokens,
                "temperature":temperature
            }
        )
        generated_text = output['response']
        logging.info(f"Completed text completion. Beginning of generated text: \n'{generated_text[:150]}'...")
        return generated_text

async def process_chunk(chunk: str, prev_context: str, chunk_index: int, total_chunks: int, reformat_as_markdown: bool, suppress_headers_and_page_numbers: bool) -> Tuple[str, str]:
    logging.info(f"Processing chunk {chunk_index + 1}/{total_chunks} (length: {len(chunk):,} characters)")
    
    # Step 1: OCR Correction
    ocr_correction_prompt = f"""Correct OCR-induced errors in the text, ensuring it flows coherently with the previous context. Follow these guidelines:

1. Fix OCR-induced typos and errors:
   - Correct words split across line breaks
   - Fix common OCR errors (e.g., 'rn' misread as 'm')
   - Use context and common sense to correct errors
   - Only fix clear errors, don't alter the content unnecessarily
   - Do not add extra periods or any unnecessary punctuation

2. Maintain original structure:
   - Keep all headings and subheadings intact

3. Preserve original content:
   - Keep all important information from the original text
   - Do not add any new information not present in the original text
   - Remove unnecessary line breaks within sentences or paragraphs
   - Maintain paragraph breaks
   
4. Maintain coherence:
   - Ensure the content connects smoothly with the previous context
   - Handle text that starts or ends mid-sentence appropriately

IMPORTANT: Respond ONLY with the corrected text. Preserve all original formatting, including line breaks. Do not include any introduction, explanation, or metadata.

Previous context:
{prev_context[-500:]}

Current chunk to process:
{chunk}

Corrected text:
"""
    
    ocr_corrected_chunk = await generate_completion(ocr_correction_prompt, max_tokens=len(chunk) + 500)
    
    processed_chunk = ocr_corrected_chunk

    # Step 2: Markdown Formatting (if requested)
    if reformat_as_markdown:
        markdown_prompt = f"""Reformat the following text as markdown, improving readability while preserving the original structure. Follow these guidelines:
1. Preserve all original headings, converting them to appropriate markdown heading levels (# for main titles, ## for subtitles, etc.)
   - Ensure each heading is on its own line
   - Add a blank line before and after each heading
2. Maintain the original paragraph structure. Remove all breaks within a word that should be a single word (for example, "cor- rect" should be "correct")
3. Format lists properly (unordered or ordered) if they exist in the original text
4. Use emphasis (*italic*) and strong emphasis (**bold**) where appropriate, based on the original formatting
5. Preserve all original content and meaning
6. Do not add any extra punctuation or modify the existing punctuation
7. Remove any spuriously inserted introductory text such as "Here is the corrected text:" that may have been added by the LLM and which is obviously not part of the original text.
8. Remove any obviously duplicated content that appears to have been accidentally included twice. Follow these strict guidelines:
   - Remove only exact or near-exact repeated paragraphs or sections within the main chunk.
   - Consider the context (before and after the main chunk) to identify duplicates that span chunk boundaries.
   - Do not remove content that is simply similar but conveys different information.
   - Preserve all unique content, even if it seems redundant.
   - Ensure the text flows smoothly after removal.
   - Do not add any new content or explanations.
   - If no obvious duplicates are found, return the main chunk unchanged.
9. {"Identify but do not remove headers, footers, or page numbers. Instead, format them distinctly, e.g., as blockquotes." if not suppress_headers_and_page_numbers else "Carefully remove headers, footers, and page numbers while preserving all other content."}

Text to reformat:

{ocr_corrected_chunk}

Reformatted markdown:
"""
        processed_chunk = await generate_completion(markdown_prompt, max_tokens=len(ocr_corrected_chunk) + 500)
    new_context = processed_chunk[-1000:]  # Use the last 1000 characters as context for the next chunk
    logging.info(f"Chunk {chunk_index + 1}/{total_chunks} processed. Output length: {len(processed_chunk):,} characters")
    return processed_chunk, new_context

async def process_chunks(chunks: List[str], reformat_as_markdown: bool, suppress_headers_and_page_numbers: bool) -> List[str]:
    total_chunks = len(chunks)
    async def process_chunk_with_context(chunk: str, prev_context: str, index: int) -> Tuple[int, str, str]:
        processed_chunk, new_context = await process_chunk(chunk, prev_context, index, total_chunks, reformat_as_markdown, suppress_headers_and_page_numbers)
        return index, processed_chunk, new_context
    if USE_LOCAL_LLM:
        logging.info("Using local LLM. Processing chunks sequentially...")
        context = ""
        processed_chunks = []
        for i, chunk in enumerate(chunks):
            processed_chunk, context = await process_chunk(chunk, context, i, total_chunks, reformat_as_markdown, suppress_headers_and_page_numbers)
            processed_chunks.append(processed_chunk)
    else:
        logging.info("Using API-based LLM. Processing chunks concurrently while maintaining order...")
        tasks = [process_chunk_with_context(chunk, "", i) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks)
        # Sort results by index to maintain order
        sorted_results = sorted(results, key=lambda x: x[0])
        processed_chunks = [chunk for _, chunk, _ in sorted_results]
    logging.info(f"All {total_chunks} chunks processed successfully")
    return processed_chunks

async def process_document(list_of_extracted_text_strings: List[str], reformat_as_markdown: bool = True, suppress_headers_and_page_numbers: bool = True) -> str:
    logging.info(f"Starting document processing. Total pages: {len(list_of_extracted_text_strings):,}")
    full_text = "\n\n".join(list_of_extracted_text_strings)
    logging.info(f"Size of full text before processing: {len(full_text):,} characters")
    chunk_size, overlap = 8000, 10
    # Improved chunking logic
    paragraphs = re.split(r'\n\s*\n', full_text)
    chunks = []
    current_chunk = []
    current_chunk_length = 0
    for paragraph in paragraphs:
        paragraph_length = len(paragraph)
        if current_chunk_length + paragraph_length <= chunk_size:
            current_chunk.append(paragraph)
            current_chunk_length += paragraph_length
        else:
            # If adding the whole paragraph exceeds the chunk size,
            # we need to split the paragraph into sentences
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
            sentences = re.split(r'(?<=[.!?])\s+', paragraph)
            current_chunk = []
            current_chunk_length = 0
            for sentence in sentences:
                sentence_length = len(sentence)
                if current_chunk_length + sentence_length <= chunk_size:
                    current_chunk.append(sentence)
                    current_chunk_length += sentence_length
                else:
                    if current_chunk:
                        chunks.append(" ".join(current_chunk))
                    current_chunk = [sentence]
                    current_chunk_length = sentence_length
    # Add any remaining content as the last chunk
    if current_chunk:
        chunks.append("\n\n".join(current_chunk) if len(current_chunk) > 1 else current_chunk[0])
    # Add overlap between chunks
    for i in range(1, len(chunks)):
        overlap_text = chunks[i-1].split()[-overlap:]
        chunks[i] = " ".join(overlap_text) + " " + chunks[i]
    logging.info(f"Document split into {len(chunks):,} chunks. Chunk size: {chunk_size:,}, Overlap: {overlap:,}")
    processed_chunks = await process_chunks(chunks, reformat_as_markdown, suppress_headers_and_page_numbers)
    final_text = "".join(processed_chunks)
    logging.info(f"Size of text after combining chunks: {len(final_text):,} characters")
    logging.info(f"Document processing complete. Final text length: {len(final_text):,} characters")
    return final_text

def remove_corrected_text_header(text):
    return text.replace("# Corrected text\n", "").replace("# Corrected text:", "").replace("\nCorrected text", "").replace("Corrected text:", "")

async def assess_output_quality(original_text, processed_text):
    max_chars = 15000  # Limit to avoid exceeding token limits
    available_chars_per_text = max_chars // 2  # Split equally between original and processed

    original_sample = original_text[:available_chars_per_text]
    processed_sample = processed_text[:available_chars_per_text]
    
    prompt = f"""Compare the following samples of original OCR text with the processed output and assess the quality of the processing. Consider the following factors:
1. Accuracy of error correction
2. Improvement in readability
3. Preservation of original content and meaning
4. Appropriate use of markdown formatting (if applicable)
5. Removal of hallucinations or irrelevant content

Original text sample:
```
{original_sample}
```

Processed text sample:
```
{processed_sample}
```

Provide a quality score between 0 and 100, where 100 is perfect processing. Also provide a brief explanation of your assessment.

Your response should be in the following format:
SCORE: [Your score]
EXPLANATION: [Your explanation]
"""

    response = await generate_completion(prompt, max_tokens=1000)
    
    try:
        lines = response.strip().split('\n')
        score_line = next(line for line in lines if line.startswith('SCORE:'))
        score = int(score_line.split(':')[1].strip())
        explanation = '\n'.join(line for line in lines if line.startswith('EXPLANATION:')).replace('EXPLANATION:', '').strip()
        logging.info(f"Quality assessment: Score {score}/100")
        logging.info(f"Explanation: {explanation}")
        return score, explanation
    except Exception as e:
        logging.error(f"Error parsing quality assessment response: {e}")
        logging.error(f"Raw response: {response}")
        return None, None
    
def ocr_file(file_path: str, lang: str = 'en', use_gpu: bool = False, page_num: int = 0) -> str:
    ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu, page_num=page_num)
    result = ocr.ocr(file_path, cls=True)
    lines = []
    for res in result:
        if res == None: # Skip when empty result detected to avoid TypeError:NoneType
            logging.debug(f"[DEBUG] Empty page detected, skip it.")
            continue
        for line in res:
            lines.append(line[1][0])
    
    return " ".join(lines)
    
async def main():
    try:
        # Suppress HTTP request logs
        logging.getLogger("httpx").setLevel(logging.WARNING)
        input_file_path = 'FILE.png'
        page_num = 0 # first n pages or 0 for all pages
        reformat_as_markdown = True
        suppress_headers_and_page_numbers = True
        
        # Download the model if using local LLM
        if USE_LOCAL_LLM:
            download_status = download_model()
            logging.info(f"Model download status: {download_status}")
            logging.info(f"Using Local LLM with Model: {DEFAULT_LOCAL_MODEL_NAME}")
        else:
            logging.info(f"Using API for completions: {API_PROVIDER}")
            logging.info(f"Using OpenAI model for embeddings: {OPENAI_EMBEDDING_MODEL}")

        base_name = os.path.splitext(input_file_path)[0]
        output_extension = '.md' if reformat_as_markdown else '.txt'
        
        raw_ocr_output_file_path = f"{base_name}__raw_ocr_output.txt"
        llm_corrected_output_file_path = base_name + '_llm_corrected' + output_extension

        # list_of_scanned_images = convert_pdf_to_images(input_pdf_file_path, max_test_pages, skip_first_n_pages)
        # logging.info("Extracting text from converted pages...")
        # with ThreadPoolExecutor() as executor:
        #     list_of_extracted_text_strings = list(executor.map(ocr_image, list_of_scanned_images))
        extracted_text = ocr_file(input_file_path, page_num=page_num)
        logging.info("Done extracting text from file.")
        with open(raw_ocr_output_file_path, "w") as f:
            f.write(extracted_text)
        logging.info(f"Raw OCR output written to: {raw_ocr_output_file_path}")

        logging.info("Processing document...")
        final_text = await process_document(extracted_text, reformat_as_markdown, suppress_headers_and_page_numbers)            
        cleaned_text = remove_corrected_text_header(final_text)
        
        # Save the LLM corrected output
        with open(llm_corrected_output_file_path, 'w') as f:
            f.write(cleaned_text)
        logging.info(f"LLM Corrected text written to: {llm_corrected_output_file_path}") 

        if final_text:
            logging.info(f"First 500 characters of LLM corrected processed text:\n{final_text[:500]}...")
        else:
            logging.warning("final_text is empty or not defined.")

        logging.info(f"Done processing {input_file_path}.")
        logging.info("\nSee output files:")
        logging.info(f" Raw OCR: {raw_ocr_output_file_path}")
        logging.info(f" LLM Corrected: {llm_corrected_output_file_path}")

        # Perform a final quality check
        quality_score, explanation = await assess_output_quality(extracted_text, final_text)
        if quality_score is not None:
            logging.info(f"Final quality score: {quality_score}/100")
            logging.info(f"Explanation: {explanation}")
        else:
            logging.warning("Unable to determine final quality score.")
    except Exception as e:
        logging.error(f"An error occurred in the main function: {e}")
        logging.error(traceback.format_exc())
        
if __name__ == '__main__':
    asyncio.run(main())
