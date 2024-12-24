"""
Core functionality for Django SEO Optimizer
Created by avixiii (https://avixiii.com)
"""
from typing import Any, Dict, List, Optional, Type, TypeVar, Union, Protocol
from dataclasses import dataclass
import hashlib
from functools import cached_property
import asyncio
from concurrent.futures import ThreadPoolExecutor

from django.db import models
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from django.template import Template, Context
from django.utils.safestring import mark_safe
from django.contrib.sites.models import Site
from django.utils.encoding import iri_to_uri
from django.conf import settings

from .utils import NotSet, Literal
from .exceptions import MetadataValidationError

T = TypeVar('T', bound='MetadataBase')


class AsyncCapable(Protocol):
    """Protocol for objects that can be processed asynchronously"""
    async def async_process(self) -> Any:
        """Process the object asynchronously"""
        ...


@dataclass
class MetadataOptions:
    """Configuration options for metadata"""
    use_cache: bool = True
    use_sites: bool = True
    use_i18n: bool = True
    use_subdomains: bool = False
    cache_prefix: str = "seo_optimizer"
    cache_timeout: int = getattr(settings, 'SEO_CACHE_TIMEOUT', 3600)  # 1 hour
    async_enabled: bool = getattr(settings, 'SEO_ASYNC_ENABLED', True)
    max_async_workers: int = getattr(settings, 'SEO_MAX_ASYNC_WORKERS', 10)


class FormattedMetadata:
    """
    Provides convenient access to formatted metadata with caching and async support.
    """
    def __init__(
        self,
        metadata: 'MetadataBase',
        instances: List[Any],
        path: str,
        site: Optional[Site] = None,
        language: Optional[str] = None,
        subdomain: Optional[str] = None
    ):
        self.__metadata = metadata
        self.__instances_original = instances
        self.__instances_cache: List[Any] = []
        self.__executor = ThreadPoolExecutor(
            max_workers=metadata._meta.max_async_workers
        ) if metadata._meta.async_enabled else None
        
        if metadata._meta.use_cache:
            self.__cache_key = self._generate_cache_key(path, site, language, subdomain)
        else:
            self.__cache_key = None

    def _generate_cache_key(
        self,
        path: str,
        site: Optional[Site],
        language: Optional[str],
        subdomain: Optional[str]
    ) -> str:
        """Generate a unique cache key for the metadata"""
        if self.__metadata._meta.use_sites and site:
            base_path = site.domain + path
        else:
            base_path = path
            
        key_parts = [
            self.__metadata._meta.cache_prefix,
            self.__metadata.__class__.__name__,
            hashlib.md5(iri_to_uri(base_path).encode('utf-8')).hexdigest()
        ]
        
        if self.__metadata._meta.use_i18n and language:
            key_parts.append(language)
            
        if self.__metadata._meta.use_subdomains and subdomain:
            key_parts.append(subdomain)
            
        return '.'.join(key_parts)

    async def async_get_attr(self, name: str) -> Any:
        """
        Asynchronously retrieve metadata value
        """
        if not self.__metadata._meta.async_enabled:
            return self.__getattr__(name)

        if self.__cache_key:
            cached_value = await self._async_cache_get(f"{self.__cache_key}.{name}")
            if cached_value is not None:
                return cached_value

        value = await self._async_resolve_value(name)
        
        if self.__cache_key:
            await self._async_cache_set(
                f"{self.__cache_key}.{name}",
                value,
                timeout=self.__metadata._meta.cache_timeout
            )
        
        return value

    async def _async_cache_get(self, key: str) -> Any:
        """Asynchronously get value from cache"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.__executor,
            cache.get,
            key
        )

    async def _async_cache_set(self, key: str, value: Any, timeout: int) -> None:
        """Asynchronously set value in cache"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.__executor,
            cache.set,
            key,
            value,
            timeout
        )

    async def _async_resolve_value(self, name: str) -> Any:
        """Asynchronously resolve metadata value"""
        for instance in self.__instances_original:
            if isinstance(instance, AsyncCapable):
                value = await instance.async_process()
            else:
                value = instance._resolve_value(name)
            if value:
                return value

        # Check for populate_from
        if name in self.__metadata._meta.elements:
            element = self.__metadata._meta.elements[name]
            populate_from = element.populate_from
            
            if callable(populate_from):
                if asyncio.iscoroutinefunction(populate_from):
                    return await populate_from(None)
                return populate_from(None)
            elif isinstance(populate_from, Literal):
                return populate_from.value
            elif populate_from is not NotSet:
                return await self._async_resolve_value(populate_from)
                
        return None

    def __getattr__(self, name: str) -> Any:
        """
        Synchronously retrieve metadata value
        """
        if self.__cache_key:
            cached_value = cache.get(f"{self.__cache_key}.{name}")
            if cached_value is not None:
                return cached_value

        value = self._resolve_value(name)
        
        if self.__cache_key:
            cache.set(
                f"{self.__cache_key}.{name}",
                value,
                timeout=self.__metadata._meta.cache_timeout
            )
        
        return value

    def _resolve_value(self, name: str) -> Any:
        """Resolve metadata value synchronously"""
        for instance in self.__instances_original:
            value = instance._resolve_value(name)
            if value:
                return value

        if name in self.__metadata._meta.elements:
            element = self.__metadata._meta.elements[name]
            populate_from = element.populate_from
            
            if callable(populate_from):
                return populate_from(None)
            elif isinstance(populate_from, Literal):
                return populate_from.value
            elif populate_from is not NotSet:
                return self._resolve_value(populate_from)
                
        return None


class MetadataBase:
    """
    Base class for all metadata definitions with async support
    """
    _meta: MetadataOptions
    
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._meta = MetadataOptions()
        
    @classmethod
    async def async_get_metadata(
        cls: Type[T],
        path: str,
        context: Optional[Dict[str, Any]] = None,
        site: Optional[Union[Site, str]] = None,
        language: Optional[str] = None,
        subdomain: Optional[str] = None
    ) -> FormattedMetadata:
        """
        Asynchronously get formatted metadata
        """
        instances = await cls._async_get_instances(path, context, site, language, subdomain)
        return FormattedMetadata(cls, instances, path, site, language, subdomain)

    @classmethod
    def get_metadata(
        cls: Type[T],
        path: str,
        context: Optional[Dict[str, Any]] = None,
        site: Optional[Union[Site, str]] = None,
        language: Optional[str] = None,
        subdomain: Optional[str] = None
    ) -> FormattedMetadata:
        """
        Synchronously get formatted metadata
        """
        instances = cls._get_instances(path, context, site, language, subdomain)
        return FormattedMetadata(cls, instances, path, site, language, subdomain)

    @classmethod
    async def _async_get_instances(
        cls: Type[T],
        path: str,
        context: Optional[Dict[str, Any]] = None,
        site: Optional[Union[Site, str]] = None,
        language: Optional[str] = None,
        subdomain: Optional[str] = None
    ) -> List[Any]:
        """Get metadata instances asynchronously"""
        raise NotImplementedError("Subclasses must implement _async_get_instances")

    @classmethod
    def _get_instances(
        cls: Type[T],
        path: str,
        context: Optional[Dict[str, Any]] = None,
        site: Optional[Union[Site, str]] = None,
        language: Optional[str] = None,
        subdomain: Optional[str] = None
    ) -> List[Any]:
        """Get metadata instances synchronously"""
        raise NotImplementedError("Subclasses must implement _get_instances")