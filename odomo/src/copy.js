import {createContext, useContext} from 'react';
import {LANGUAGES} from './lib/i18n.js';

// The current language's table, for every component that draws words.
export const Copy = createContext({t: LANGUAGES.en, lang: 'en'});
export const useCopy = () => useContext(Copy);
