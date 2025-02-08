"""Product data processor."""

from datetime import datetime
from typing import Dict, Any, List, Set, Optional
from pathlib import Path
import logging
import pandas as pd

from ..db.session import SessionManager
from ..utils import generate_uuid
from ..utils.system_products import initialize_system_products, is_system_product
from ..db.models import Product
from .base import BaseProcessor
from .error_tracker import ErrorTracker

class ProductProcessor(BaseProcessor):
    """Process products from sales data."""
    
    def __init__(self, session_manager: SessionManager, batch_size: int = 100):
        """Initialize the processor.
        
        Args:
            session_manager: Database session manager
            batch_size: Number of products to process per batch
        """
        self.session_manager = session_manager
        super().__init__(None, batch_size)  # We'll manage sessions ourselves
        
        # Initialize error tracker
        self.error_tracker = ErrorTracker()
        
        # Field mappings from CSV to our schema
        self.field_mappings = {
            'product_code': ['Product/Service'],
            'description': ['Product/Service Description']
        }
        
        # Track processed products across batches
        self.processed_codes: Set[str] = set()
        
        # Additional stats
        self.stats.update({
            'total_products': 0,
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'validation_errors': 0
        })
        
    def process_file(self, file_path: Path) -> Dict[str, Any]:
        """Process products from a CSV file.
        
        Args:
            file_path: Path to the CSV file
            
        Returns:
            Dict containing processing results
        """
        try:
            # Initialize system products first
            with self.session_manager as session:
                self.logger.debug("Initializing system products...")
                initialize_system_products(session)
                self.logger.debug("System products initialized")
            
            # Read CSV into DataFrame
            df = pd.read_csv(file_path)
            
            # Map CSV headers to our standardized field names
            header_mapping = {}
            for std_field, possible_names in self.field_mappings.items():
                for name in possible_names:
                    if name in df.columns:
                        header_mapping[std_field] = name
                        break
            
            if not all(field in header_mapping for field in self.field_mappings.keys()):
                raise ValueError("Missing required columns in CSV file")
            
            # Process in batches
            total_batches = (len(df) + self.batch_size - 1) // self.batch_size
            print(f"\nProcessing {len(df)} rows in batches of {self.batch_size}", flush=True)
            
            for batch_num, start_idx in enumerate(range(0, len(df), self.batch_size), 1):
                batch_df = df.iloc[start_idx:start_idx + self.batch_size]
                
                try:
                    with self.session_manager as session:
                        # Process batch
                        for _, row in batch_df.iterrows():
                            self._process_row(row, header_mapping, session)
                        
                        # Commit batch
                        session.commit()
                        self.stats['successful_batches'] += 1
                        
                except Exception as e:
                    self.stats['failed_batches'] += 1
                    self.stats['total_errors'] += 1
                    logging.error(f"Error processing batch {batch_num}: {str(e)}")
                    continue
                
                # Print progress
                print(f"Batch {batch_num}/{total_batches} complete", flush=True)
                print(f"Products: {self.stats['total_products']} total, "
                      f"{self.stats['created']} created, "
                      f"{self.stats['updated']} updated", flush=True)
            
            # Log error summary
            self.error_tracker.log_summary(self.logger)
            
            return {
                'success': self.stats['failed_batches'] == 0,
                'summary': {
                    'stats': self.stats,
                    'errors': self.error_tracker.get_summary()
                }
            }
            
        except Exception as e:
            logging.error(f"Failed to process file: {str(e)}")
            self.error_tracker.add_error(
                'file_processing',
                str(e),
                {'file': str(file_path)}
            )
            return {
                'success': False,
                'summary': {
                    'stats': self.stats,
                    'errors': self.error_tracker.get_summary()
                }
            }
    
    def _validate_product_code(self, product_code: str) -> Optional[str]:
        """Validate product code format.
        
        Args:
            product_code: Product code to validate
            
        Returns:
            None if valid, error message if invalid
        """
        if not product_code:
            return "Product code is required"
        if len(product_code) > 50:  # Arbitrary limit
            return "Product code exceeds maximum length"
        # Allow alphanumeric, hyphen, underscore, and period
        if not all(c.isalnum() or c in '-_.' for c in product_code):
            return "Product code contains invalid characters (only letters, numbers, hyphen, underscore, and period allowed)"
        return None
        
    def _validate_description(self, description: str) -> Optional[str]:
        """Validate product description.
        
        Args:
            description: Product description to validate
            
        Returns:
            None if valid, error message if invalid
        """
        if not description:
            return None  # Description is optional
        if len(description) > 500:  # Arbitrary limit
            return "Description exceeds maximum length"
        return None
        
    def _validate_product_data(self, product_code: str, description: str) -> List[str]:
        """Validate all product data fields.
        
        Args:
            product_code: Product code to validate
            description: Product description to validate
            
        Returns:
            List of validation error messages
        """
        errors = []
        
        # Check product code
        code_error = self._validate_product_code(product_code)
        if code_error:
            errors.append(code_error)
            
        # Check description
        desc_error = self._validate_description(description)
        if desc_error:
            errors.append(desc_error)
            
        # Business rules
        if product_code.startswith('TEST-'):
            errors.append("Test products not allowed in production")
            
        if description and description.lower().startswith('deprecated'):
            errors.append("Deprecated products should not be imported")
            
        return errors

    def _process_row(self, row: pd.Series, header_mapping: Dict[str, str], session) -> None:
        """Process a single product row."""
        try:
            # Get and validate product data
            product_code = str(row[header_mapping['product_code']]).strip().upper()
            description = str(row[header_mapping['description']]).strip()
            
            validation_errors = self._validate_product_data(product_code, description)
            if validation_errors:
                self.stats['validation_errors'] += len(validation_errors)
                for error in validation_errors:
                    self.error_tracker.add_error(
                        'validation',
                        error,
                        {'row': row.to_dict()}
                    )
                return
            
            # Skip duplicates and system products (already initialized)
            if product_code in self.processed_codes:
                self.stats['skipped'] += 1
                return
            if is_system_product(product_code):
                self.stats['skipped'] += 1
                return
            self.processed_codes.add(product_code)
            
            self.stats['total_products'] += 1
            
            # Get or create product
            product = session.query(Product).filter(
                Product.productCode == product_code
            ).first()
            
            now = datetime.utcnow()
            
            if product:
                # Update existing product
                if description and description != product.description:
                    product.description = description
                    product.modifiedAt = now
                    self.stats['updated'] += 1
                else:
                    # No changes needed
                    self.stats['skipped'] += 1
            else:
                # Create new product
                product = Product(
                    id=generate_uuid(),
                    productCode=product_code,
                    name=product_code,  # Use code as name initially
                    description=description,
                    createdAt=now,
                    modifiedAt=now
                )
                session.add(product)
                self.stats['created'] += 1
                
        except Exception as e:
            self.error_tracker.add_error(
                'processing',
                str(e),
                {'row': row.to_dict()}
            )
            self.stats['total_errors'] += 1
