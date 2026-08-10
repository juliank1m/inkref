"""Optional semantic layer.

The model decides *what* a region is. The geometry engine decides *where* it goes. The
stroke engine moves the original ink. Nothing here ever produces a coordinate.
"""
from .analyzer import (BackboardAnalyzer, HeuristicAnalyzer, SemanticResult,  # noqa: F401
                       get_analyzer)
from .schemas import Block, BLOCK_TYPES, InvalidModelOutput  # noqa: F401
