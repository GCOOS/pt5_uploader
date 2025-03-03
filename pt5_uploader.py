#!/usr/bin/env python3
"""
PT5 Uploader - A tool for uploading Imaging FlowCytobot (IFCB) data files to 
Amazon S3.

This script is specifically designed for the GCOOS ORION project to manage and 
upload IFCB (Imaging FlowCytobot) data files to Amazon S3. IFCB is an automated 
submersible flow cytometer that provides continuous, high-resolution measurements 
of phytoplankton and microzooplankton abundance and composition.

Features:
    - AWS credentials validation
    - Support for IFCB data file uploads
    - Recursive directory upload option
    - Colorized console output
    - Concurrent file uploads (up to 32 workers)
    - Connection pool optimization (100 connections)
    - Automatic retry on failures (3 attempts)
    - Batched file submission (1000 files per batch)
    - Pre-computed S3 keys for improved performance
    - Overall progress tracking with tqdm
    - Detailed summary report with:
        * Total files processed
        * Total data transferred
        * Upload duration
        * Average transfer rate
        * Files processed per second
    - Dry-run mode for testing
    - Environment variable configuration
    - Detailed logging

System Requirements:
    - Python 3.6 or higher
    - Sufficient system resources for concurrent processing
    - Recommended: 4+ CPU cores and 8GB+ RAM for large file sets

Author: robertdcurrier@tamu.edu
"""

import argparse
import logging
import os
import sys
import time
from typing import Optional, List, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.config import Config
from colorama import Fore, Style, init
from tqdm import tqdm
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Initialize colorama for cross-platform colored output
init(autoreset=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def validate_aws_credentials() -> bool:
    """
    Validate AWS credentials by attempting to list S3 buckets.
    
    Returns:
        bool: True if credentials are valid, False otherwise
    """
    try:
        s3_client = boto3.client('s3')
        s3_client.list_buckets()
        logger.info(f"{Fore.GREEN}AWS credentials validated successfully"
                   f"{Style.RESET_ALL}")
        return True
    except NoCredentialsError:
        logger.error(f"{Fore.RED}Error: AWS credentials not found. Please "
                    f"check your .env file for AWS_ACCESS_KEY_ID and "
                    f"AWS_SECRET_ACCESS_KEY{Style.RESET_ALL}")
        return False
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'InvalidAccessKeyId':
            logger.error(f"{Fore.RED}Error: Invalid AWS Access Key ID"
                        f"{Style.RESET_ALL}")
        elif error_code == 'SignatureDoesNotMatch':
            logger.error(f"{Fore.RED}Error: Invalid AWS Secret Access Key"
                        f"{Style.RESET_ALL}")
        else:
            logger.error(f"{Fore.RED}Error: AWS credentials validation failed: "
                        f"{str(e)}{Style.RESET_ALL}")
        return False
    except Exception as e:
        logger.error(f"{Fore.RED}Error: Unexpected error validating AWS "
                    f"credentials: {str(e)}{Style.RESET_ALL}")
        return False

def get_default_source() -> Optional[str]:
    """Get the default source directory from environment variables."""
    ifcb_dir = os.getenv('IFCB_DATA_DIR')
    if ifcb_dir and os.path.exists(ifcb_dir):
        return ifcb_dir
    return None

def get_default_bucket() -> Optional[str]:
    """Get the default bucket from AWS_UPLOAD_URL environment variable."""
    upload_url = os.getenv('AWS_UPLOAD_URL', '')
    if upload_url.startswith('s3://'):
        # Extract bucket from s3://bucket/path format
        parts = upload_url[5:].split('/')
        return parts[0]
    return None

def get_default_prefix() -> str:
    """Get the default prefix from AWS_UPLOAD_URL environment variable."""
    upload_url = os.getenv('AWS_UPLOAD_URL', '')
    if upload_url.startswith('s3://'):
        # Extract path after bucket
        parts = upload_url[5:].split('/')
        if len(parts) > 1:
            return '/'.join(parts[1:])
    return ''

def setup_argparse() -> argparse.ArgumentParser:
    """Set up and return the argument parser with all required options."""
    default_source = get_default_source()
    default_bucket = get_default_bucket()
    default_prefix = get_default_prefix()

    parser = argparse.ArgumentParser(
        description='Upload files to Amazon S3 with progress tracking'
    )
    parser.add_argument(
        '--source',
        help='Source file or directory to upload',
        default=default_source
    )
    parser.add_argument(
        '--bucket',
        help='Target S3 bucket name',
        default=default_bucket
    )
    parser.add_argument(
        '--prefix',
        help='S3 key prefix (optional)',
        default=default_prefix
    )
    parser.add_argument(
        '--recursive',
        action='store_true',
        help='Upload directories recursively'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be uploaded without actually uploading'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Only validate AWS credentials and exit'
    )
    return parser

def validate_args(args: argparse.Namespace) -> bool:
    """Validate command line arguments."""
    if not args.source:
        logger.error(f"{Fore.RED}Error: Source path not specified and "
                    f"IFCB_DATA_DIR not set{Style.RESET_ALL}")
        return False
    if not os.path.exists(args.source):
        logger.error(f"{Fore.RED}Error: Source path does not exist: "
                    f"{args.source}{Style.RESET_ALL}")
        return False
    if not args.bucket:
        logger.error(f"{Fore.RED}Error: Bucket not specified and "
                    f"AWS_UPLOAD_URL not set{Style.RESET_ALL}")
        return False
    return True

def get_files_to_upload(source: str, recursive: bool = False) -> List[Tuple[str, str]]:
    """
    Get list of files to upload and their S3 keys.
    
    Args:
        source: Source file or directory path
        recursive: Whether to include subdirectories
        
    Returns:
        List of tuples containing (local_path, s3_key)
    """
    source_path = Path(source)
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source}")
        
    if source_path.is_file():
        return [(str(source_path), source_path.name)]
        
    files = []
    pattern = "**/*" if recursive else "*"
    
    for file_path in source_path.glob(pattern):
        if file_path.is_file():
            # Get relative path for S3 key
            rel_path = file_path.relative_to(source_path)
            files.append((str(file_path), str(rel_path)))
            
    return files

def upload_file(s3_client: boto3.client, local_path: str, bucket: str, 
                s3_key: str, dry_run: bool = False) -> bool:
    """
    Upload a single file to S3.
    
    Args:
        s3_client: Boto3 S3 client
        local_path: Local file path
        bucket: S3 bucket name
        s3_key: S3 key (path in bucket)
        dry_run: Whether to simulate upload
        
    Returns:
        bool: True if upload successful, False otherwise
    """
    try:
        if dry_run:
            logger.info(f"{Fore.CYAN}Would upload: {local_path} -> "
                       f"s3://{bucket}/{s3_key}{Style.RESET_ALL}")
            return True
            
        logger.debug(f"{Fore.CYAN}Starting upload of {local_path}{Style.RESET_ALL}")
        with open(local_path, 'rb') as f:
            s3_client.upload_fileobj(f, bucket, s3_key)
        logger.debug(f"{Fore.GREEN}Completed upload of {local_path}{Style.RESET_ALL}")
        return True
        
    except Exception as e:
        logger.error(f"{Fore.RED}Error uploading {local_path}: {str(e)}"
                    f"{Style.RESET_ALL}")
        return False

def format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable string."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

def print_summary_report(total_files: int, total_size: int, start_time: float) -> None:
    """
    Print a summary report of the upload process.
    
    Args:
        total_files: Total number of files uploaded
        total_size: Total size of files in bytes
        start_time: Start time of upload process
    """
    end_time = time.time()
    duration = end_time - start_time
    avg_rate = total_size / duration if duration > 0 else 0
    files_per_second = total_files / duration if duration > 0 else 0
    
    logger.info(f"\n{Fore.GREEN}Upload Summary Report:{Style.RESET_ALL}")
    logger.info(f"{Fore.CYAN}Total Files:{Style.RESET_ALL} {total_files}")
    logger.info(f"{Fore.CYAN}Total Size:{Style.RESET_ALL} {format_size(total_size)}")
    logger.info(f"{Fore.CYAN}Duration:{Style.RESET_ALL} {duration:.2f} seconds")
    logger.info(f"{Fore.CYAN}Average Rate:{Style.RESET_ALL} {format_size(avg_rate)}/s")
    logger.info(f"{Fore.CYAN}Files/Second:{Style.RESET_ALL} {files_per_second:.2f}")

def upload_files(args: argparse.Namespace) -> bool:
    """
    Upload files to S3 based on command line arguments.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        bool: True if all uploads successful, False otherwise
    """
    try:
        # Configure S3 client with larger connection pool and timeouts
        session = boto3.Session()
        config = Config(
            max_pool_connections=100,  # Increase connection pool size
            retries={'max_attempts': 3},  # Add retry configuration
            connect_timeout=5,  # Connection timeout in seconds
            read_timeout=60,    # Read timeout in seconds
            tcp_keepalive=True  # Enable TCP keepalive
        )
        s3_client = session.client('s3', config=config)
        
        files = get_files_to_upload(args.source, args.recursive)
        
        if not files:
            logger.warning(f"{Fore.YELLOW}No files found to upload{Style.RESET_ALL}")
            return True
            
        if args.dry_run:
            logger.info(f"{Fore.CYAN}Found {len(files)} files to upload:"
                       f"{Style.RESET_ALL}")
            for local_path, s3_key in files:
                # Combine prefix and s3_key without extra slashes
                full_s3_key = os.path.join(args.prefix, s3_key).replace('\\', '/')
                logger.info(f"{Fore.CYAN}  {local_path} -> "
                           f"s3://{args.bucket}/{full_s3_key}"
                           f"{Style.RESET_ALL}")
            return True
            
        # Log initial file count
        total_files = len(files)
        logger.info(f"{Fore.CYAN}Found {total_files} files to upload{Style.RESET_ALL}")
            
        success = True
        max_workers = min(32, total_files)  # Limit to 32 concurrent uploads
        logger.info(f"{Fore.CYAN}Using {max_workers} concurrent workers{Style.RESET_ALL}")
        
        # Initialize statistics
        total_size = 0
        start_time = time.time()
        
        # Pre-compute all S3 keys
        logger.info(f"{Fore.CYAN}Preparing upload tasks...{Style.RESET_ALL}")
        upload_tasks = []
        for local_path, s3_key in files:
            full_s3_key = os.path.join(args.prefix, s3_key).replace('\\', '/')
            upload_tasks.append((local_path, full_s3_key))
        
        logger.info(f"{Fore.CYAN}Starting ThreadPoolExecutor with {max_workers} workers{Style.RESET_ALL}")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {}
            
            # Show progress during file submission
            with tqdm(total=total_files, desc="Submitting files", unit="file") as submit_pbar:
                # Submit files in batches of 1000
                batch_size = 1000
                for i in range(0, len(upload_tasks), batch_size):
                    batch = upload_tasks[i:i + batch_size]
                    # Submit batch of files
                    futures = [
                        executor.submit(
                            upload_file, s3_client, local_path, args.bucket,
                            s3_key, args.dry_run
                        ) for local_path, s3_key in batch
                    ]
                    # Map futures to file paths
                    for future, (local_path, _) in zip(futures, batch):
                        future_to_file[future] = local_path
                    submit_pbar.update(len(batch))
                
            logger.info(f"{Fore.CYAN}All uploads submitted, waiting for completion{Style.RESET_ALL}")
            
            # Create progress bar for upload completion
            with tqdm(total=total_files, desc="Uploading files", unit="file") as pbar:
                for future in as_completed(future_to_file):
                    file_path = future_to_file[future]
                    try:
                        if not future.result():
                            success = False
                        # Add file size to total
                        file_size = os.path.getsize(file_path)
                        total_size += file_size
                        pbar.update(1)  # Update progress after each file
                        logger.debug(f"{Fore.GREEN}Successfully uploaded {file_path}{Style.RESET_ALL}")
                    except Exception as e:
                        logger.error(f"{Fore.RED}Error uploading {file_path}: {str(e)}"
                                   f"{Style.RESET_ALL}")
                        success = False
                        pbar.update(1)  # Update progress even on error
                        
        # Print summary report
        print_summary_report(total_files, total_size, start_time)
        return success
        
    except Exception as e:
        logger.error(f"{Fore.RED}Error during upload process: {str(e)}"
                    f"{Style.RESET_ALL}")
        return False

def main() -> int:
    """Main entry point for the application."""
    parser = setup_argparse()
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # If --validate is set, only check credentials and exit
    if args.validate:
        logger.info(f"{Fore.CYAN}Validating AWS credentials...{Style.RESET_ALL}")
        return 0 if validate_aws_credentials() else 1

    if not validate_args(args):
        return 1

    if not validate_aws_credentials():
        return 1

    try:
        logger.info(f"{Fore.GREEN}Starting upload process...{Style.RESET_ALL}")
        success = upload_files(args)
        return 0 if success else 1
    except Exception as e:
        logger.error(f"{Fore.RED}Error: {str(e)}{Style.RESET_ALL}")
        return 1

if __name__ == '__main__':
    sys.exit(main())
