import logging
import uuid
from typing import Type, List, Optional, Any, Iterable, TYPE_CHECKING, Tuple

from langchain.docstore.document import Document
from langchain.schema.embeddings import Embeddings
from langchain.schema.vectorstore import VectorStore, VST

if TYPE_CHECKING:
    from databricks.vector_search.client import VectorSearchIndex

logger = logging.getLogger(__name__)


class DatabricksVectorSearch(VectorStore):
    """`Databricks Vector Search` vector store.

    To use, you should have the ``databricks-vectorsearch`` python package installed.

    Example:
        .. code-block:: python

            from langchain.schema.vectorstore import DatabricksVectorSearch
            from databricks.vector_search.client import VectorSearchClient

            vs_client = VectorSearchClient()
            vs_index = vs_client.get_index(
              endpoint_name="vs_endpoint",
              index_name="delta_sync_managed_embeddings_index"
            )
            vectorstore = DatabricksVectorSearch(vs_index)

    Args:
        index: A Databricks Vector Search index object.
        embedding: The embedding model.
                  Required for direct-access index or delta-sync index with self-managed embeddings.
        text_column: The name of the text column to use for the embeddings.
                    Required for direct-access index or delta-sync index with self-managed embeddings.
                    Make sure the text column specified is in the index.
        columns: The list of column names to get when doing the search. Defaults to ``[primary_key, text_column]``.

    Delta-sync index with Databricks-managed embeddings manages the ingestion, deletion, and embedding for you.
    Manually ingestion/deletion of the documents/texts is not supported for delta-sync index.

    If you want to use a delta-sync index with self-managed embeddings, you need to provide the embedding model and
    the name of the text column to use for the embeddings.

    Example:
        .. code-block:: python

            from langchain.schema.vectorstore import DatabricksVectorSearch
            from databricks.vector_search.client import VectorSearchClient
            from langchain.embeddings.openai import OpenAIEmbeddings

            vs_client = VectorSearchClient()
            vs_index = vs_client.get_index(
              endpoint_name="vs_endpoint",
              index_name="delta_sync_self_managed_embeddings_index"
            )
            vectorstore = DatabricksVectorSearch(
              index=vs_index,
              embedding=OpenAIEmbeddings(),
              text_column="document_content"
            )

    If you want to manage the documents ingestion/deletion yourself, you can use a direct-access index.

    Example:
        .. code-block:: python

            from langchain.schema.vectorstore import DatabricksVectorSearch
            from databricks.vector_search.client import VectorSearchClient
            from langchain.embeddings.openai import OpenAIEmbeddings

            vs_client = VectorSearchClient()
            vs_index = vs_client.get_index(
              endpoint_name="vs_endpoint",
              index_name="direct_access_index"
            )
            vectorstore = DatabricksVectorSearch(
              index=vs_index,
              embedding=OpenAIEmbeddings(),
              text_column="document_content"
            )
            vectorstore.add_texts(
              texts=["text1", "text2"]
            )

    For more information on Databricks Vector Search, see `Databricks Vector Search documentation
    <TODO: pending-link-to-documentation-page>`.

    """

    def __init__(
        self,
        index: "VectorSearchIndex",
        *,
        embedding: Optional[Embeddings] = None,
        text_column: Optional[str] = None,
        columns: Optional[List[str]] = None,
        **kwargs: Any,
    ):
        try:
            from databricks.vector_search.client import VectorSearchIndex
        except ImportError:
            raise ImportError(
                "Could not import databricks-vectorsearch python package. "
                "Please install it with `pip install databricks-vectorsearch`."
            )
        # index
        self.index = index
        if not isinstance(index, VectorSearchIndex):
            raise TypeError("index must be of type VectorSearchIndex.")

        # index_details
        self.index_details = self.index.describe()

        # text_column
        if self._is_databricks_managed_embeddings():
            index_source_column = self._embedding_source_column_name()
            # if text_column is not None, check that it matches the source column of the index
            if text_column is not None and text_column != index_source_column:
                raise ValueError(
                    f"text_column '{text_column}' does not match with the source column "
                    f"of the index: '{index_source_column}'."
                )
            self.text_column = index_source_column
        else:
            self._require_arg(text_column, "text_column")
            self.text_column = text_column

        # primary_key
        self.primary_key = self.index_details["primary_key"]

        # columns
        self.columns = columns or []
        # add primary key column and source column if not in columns
        if self.primary_key not in self.columns:
            self.columns.append(self.primary_key)
        if self.text_column not in self.columns:
            self.columns.append(self.text_column)

        # embedding model
        if not self._is_databricks_managed_embeddings():
            # embedding model is required for direct-access index or delta-sync index with self-managed embedding
            self._require_arg(embedding, "embedding")
            self._embedding = embedding
            # validate dimension matches
            index_embedding_dimension = self._embedding_vector_column_dimension()
            if index_embedding_dimension is not None:
                inferred_embedding_dimension = self._infer_embedding_dimension()
                if inferred_embedding_dimension != index_embedding_dimension:
                    raise ValueError(
                        f"embedding model's dimension '{inferred_embedding_dimension}' does not match with "
                        f"the index's dimension '{index_embedding_dimension}'."
                    )
        else:
            logger.warning(
                "embedding model is not used in delta-sync index with Databricks-managed embeddings."
            )
            self._embedding = None

    @classmethod
    def from_texts(
        cls: Type[VST],
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> VST:
        raise NotImplementedError(
            "`from_texts` is not supported. "
            "Use `add_texts` to add to existing direct-access index."
        )

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[Any]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Add texts to the index.

        Only support direct-access index.

        Args:
            texts: List of texts to add.
            metadatas: List of metadata for each text. Defaults to None.
            ids: List of ids for each text. Defaults to None.

        Returns:
            List of ids from adding the texts into the index.
        """
        self._op_require_direct_access_index("add_texts")
        texts = list(texts)
        vectors = self.embeddings.embed_documents(texts)
        ids = ids or [str(uuid.uuid4()) for _ in texts]
        metadatas = metadatas or [{} for _ in texts]

        updates = [
            {
                self.primary_key: id_,
                self.text_column: text,
                self._embedding_vector_column_name(): vector,
                **metadata,
            }
            for text, vector, id_, metadata in zip(texts, vectors, ids, metadatas)
        ]

        self.index.upsert(updates)

        return ids

    @property
    def embeddings(self) -> Optional[Embeddings]:
        """Access the query embedding object if available."""
        return self._embedding

    def delete(self, ids: Optional[List[Any]] = None, **kwargs: Any) -> Optional[bool]:
        """Delete documents from the index.

        Only support direct-access index.

        Args:
            ids: List of ids of documents to delete.

        Returns:
            True if successful.
        """
        self._op_require_direct_access_index("delete")
        if ids is None:
            raise ValueError("ids must be provided.")
        self.index.delete(ids)
        return True

    def similarity_search(
        self, query: str, k: int = 4, filters: Optional[Any] = None, **kwargs: Any
    ) -> List[Document]:
        """Return docs most similar to query.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filters: Filters to apply to the query. Defaults to None.

        Returns:
            List of Documents most similar to the embedding.
        """
        docs_with_score = self.similarity_search_with_score(
            query=query, k=k, filters=filters, **kwargs
        )
        return [doc for doc, _ in docs_with_score]

    def similarity_search_with_score(
        self, query: str, k: int = 4, filters: Optional[Any] = None, **kwargs: Any
    ) -> List[Tuple[Document, float]]:
        """Return docs most similar to query, along with scores.

        Args:
            query: Text to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filters: Filters to apply to the query. Defaults to None.

        Returns:
            List of Documents most similar to the embedding and score for each.
        """
        if self._is_databricks_managed_embeddings():
            query_text = query
            query_vector = None
        else:
            query_text = None
            query_vector = self.embeddings.embed_query(query)

        search_resp = self.index.similarity_search(
            columns=self.columns,
            query_text=query_text,
            query_vector=query_vector,
            filters=filters,
            num_results=k,
        )
        return self._parse_search_response(search_resp)

    def similarity_search_by_vector(
        self,
        embedding: List[float],
        k: int = 4,
        filters: Optional[Any] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs most similar to embedding vector.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filters: Filters to apply to the query. Defaults to None.

        Returns:
            List of Documents most similar to the embedding.
        """
        docs_with_score = self.similarity_search_by_vector_with_score(
            embedding=embedding, k=k, filters=filters, **kwargs
        )
        return [doc for doc, _ in docs_with_score]

    def similarity_search_by_vector_with_score(
        self,
        embedding: List[float],
        k: int = 4,
        filters: Optional[Any] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Return docs most similar to embedding vector, along with scores.

        Args:
            embedding: Embedding to look up documents similar to.
            k: Number of Documents to return. Defaults to 4.
            filters: Filters to apply to the query. Defaults to None.

        Returns:
            List of Documents most similar to the embedding and score for each.
        """
        if self._is_databricks_managed_embeddings():
            raise ValueError(
                "`similarity_search_by_vector` is not supported for index with Databricks-managed embeddings."
            )
        search_resp = self.index.similarity_search(
            columns=self.columns,
            query_vector=embedding,
            filters=filters,
            num_results=k,
        )
        return self._parse_search_response(search_resp)

    def _parse_search_response(self, search_resp: dict) -> List[Tuple[Document, float]]:
        """Parse the search response into a list of Documents with score."""
        columns = [col["name"] for col in search_resp["manifest"]["columns"]]
        docs_with_score = []
        for result in search_resp["result"]["data_array"]:
            doc_id = result[columns.index(self.primary_key)]
            text_content = result[columns.index(self.text_column)]
            metadata = {
                col: value
                for col, value in zip(columns[:-1], result[:-1])
                if col not in [self.primary_key, self.text_column]
            }
            metadata[self.primary_key] = doc_id
            score = result[-1]
            doc = Document(page_content=text_content, metadata=metadata)
            docs_with_score.append((doc, score))
        return docs_with_score

    def _embedding_vector_column_name(self) -> Optional[str]:
        """Return the name of the embedding vector column.
        None if the index is not a self-managed embedding index.
        """
        return self._embedding_vector_column().get("name")

    def _embedding_vector_column_dimension(self) -> Optional[int]:
        """Return the dimension of the embedding vector column.
        None if the index is not a self-managed embedding index.
        """
        return self._embedding_vector_column().get("embedding_dimension")

    def _embedding_vector_column(self) -> dict:
        """Return the embedding vector column configs as a dictionary.
        Empty if the index is not a self-managed embedding index.
        """
        index_spec = (
            self._delta_sync_index_spec()
            if self._is_delta_sync_index()
            else self._direct_access_index_spec()
        )
        return next(iter(index_spec.get("embedding_vector_columns") or list()), dict())

    def _embedding_source_column_name(self) -> Optional[str]:
        """Return the name of the embedding source column.
        None if the index is not a Databricks-managed embedding index.
        """
        return self._embedding_source_column().get("name")

    def _embedding_source_column(self) -> dict:
        """Return the embedding source column configs as a dictionary.
        Empty if the index is not a Databricks-managed embedding index.
        """
        index_spec = self._delta_sync_index_spec()
        return next(iter(index_spec.get("embedding_source_columns") or list()), dict())

    def _delta_sync_index_spec(self) -> dict:
        """Return the delta-sync index configs as a dictionary.
        Empty if the index is not a delta-sync index.
        """
        return self.index_details.get("delta_sync_index_spec", dict())

    def _direct_access_index_spec(self) -> dict:
        """Return the direct-access index configs as a dictionary.
        Empty if the index is not a direct-access index.
        """
        return self.index_details.get("direct_access_index_spec", dict())

    def _is_delta_sync_index(self) -> bool:
        """Return True if the index is a delta-sync index."""
        return self.index_details.get("index_type") == "DELTA_SYNC"

    def _is_direct_access_index(self) -> bool:
        """Return True if the index is a direct-access index."""
        return self.index_details.get("index_type") == "DIRECT_ACCESS"

    def _is_databricks_managed_embeddings(self) -> bool:
        """Return True if the embeddings are managed by Databricks Vector Search."""
        return (
            self._is_delta_sync_index()
            and self._embedding_source_column_name() is not None
        )

    def _infer_embedding_dimension(self) -> int:
        """Infer the embedding dimension from the embedding function."""
        return len(self.embeddings.embed_query("test"))

    def _op_require_direct_access_index(self, op_name):
        """Raise ValueError if the operation is not supported for direct-access index."""
        if not self._is_direct_access_index():
            raise ValueError(f"`{op_name}` is only supported for direct-access index.")

    @staticmethod
    def _require_arg(arg: Any, arg_name):
        """Raise ValueError if the required arg with name `arg_name` is None."""
        if not arg:
            raise ValueError(f"`{arg_name}` is required for this index.")
